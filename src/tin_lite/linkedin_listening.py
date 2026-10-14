"""LinkedIn listening: Tin's own readings of how each LinkedIn post did.

Zernio returns a post's numbers live but keeps no hourly history, so Tin takes and stores its
own readings. `social.linkedin_listen` reads one post Tin published 1, 4, 24 and 72 hours after
posting. `social.linkedin_results` runs daily: it reads every tracked post from the last 30
days, picks up page posts made directly on LinkedIn and records each account's follower count.

Postgres holds the record (`linkedin_posts`, `linkedin_post_readings`,
`linkedin_follower_counts`). After each reading Tin rewrites `social/linkedin/results.json` and
`social/linkedin/RESULTS.md` from it; both are Tin's own files. A personal profile's numbers
exist only for posts published through Zernio, so the profile's other posts never appear. See
docs/linkedin-posting.md.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from tin_lite.domain import (
    LINKEDIN_LISTEN_CHECKPOINTS,
    LINKEDIN_LISTEN_WORKFLOW_NAME,
    LINKEDIN_RESULTS_WORKFLOW_NAME,
)
from tin_lite.zernio import METRICS, post_urn

LISTEN_KEY = LINKEDIN_LISTEN_WORKFLOW_NAME
RESULTS_KEY = LINKEDIN_RESULTS_WORKFLOW_NAME
LISTEN_WORKFLOW_ID = UUID("5b0e7c2a-4f1d-4b8e-9a63-2d7f1c8e4a90")
RESULTS_WORKFLOW_ID = UUID("c3d9a1e4-7b25-4f60-8e1a-9f4b2c6d8e17")
SCHEMA = "tin.social.linkedin_results.v1"
RESULTS_JSON = "social/linkedin/results.json"
RESULTS_MD = "social/linkedin/RESULTS.md"
PATHS = {"results.json": RESULTS_JSON, "RESULTS.md": RESULTS_MD}
LIMITS = {"results.json": 512_000, "RESULTS.md": 512_000}
# The daily run reads posts for 30 days after publishing (d0 to d30).
WINDOW_DAYS = 30
# The files list the most recent posts from the last 90 days.
LISTED_DAYS = 90
MAX_LISTED = 60
EXCERPT = 280
CHECKPOINTS = dict(LINKEDIN_LISTEN_CHECKPOINTS)
# The readings the files show; every daily reading stays in Postgres.
SHOWN = ("1h", "4h", "24h", "72h", "d7", "d30")
LABELS = {
    "1h": "1 hour",
    "4h": "4 hours",
    "24h": "1 day",
    "72h": "3 days",
    "d7": "7 days",
    "d30": "30 days",
}
LISTEN_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "project_id": {"type": "string", "format": "uuid"},
        "publish_run_id": {"type": "string", "format": "uuid"},
    },
    "required": ["project_id", "publish_run_id"],
}
RESULTS_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"project_id": {"type": "string", "format": "uuid"}},
    "required": ["project_id"],
}
INTRO = (
    "Tin reads each LinkedIn post it publishes 1, 4, 24 and 72 hours after posting, and the "
    "daily results run reads every post again for 30 days. Page posts made directly on "
    "LinkedIn join at the next daily run. LinkedIn shares no numbers for posts made on a "
    "personal profile outside Tin. Tin rewrites this file after each reading."
)


def listen_start_key(publish_run_id) -> str:
    return f"linkedin-listen:{UUID(str(publish_run_id))}"


def daily_label(published_at: datetime, now: datetime) -> str:
    return f"d{min(max((now - published_at).days, 0), WINDOW_DAYS)}"


def counts(values: dict[str, Any]) -> str:
    """A reading in a few words, for progress lines and run summaries."""

    parts = [f"{values.get('impressions', 0):,} impressions"]
    for name in ("reactions", "comments"):
        if values.get(name):
            number = values[name]
            parts.append(f"{number:,} {name if number != 1 else name[:-1]}")
    return ", ".join(parts)


def excerpt(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= EXCERPT else text[: EXCERPT - 1].rstrip() + "…"


async def published_post(db, run, *, conn=None) -> dict[str, Any] | None:
    """The post a succeeded social.linkedin_publish run made, as listening tracks it."""

    mirror = await db.get_effect(f"{run.id}:linkedin_post", conn=conn)
    value = mirror.result if mirror and mirror.status == "completed" else None
    approval_id = (run.input or {}).get("approval_id")
    approval = (
        await db.get_effect(f"linkedin:approved:{UUID(str(approval_id))}", conn=conn)
        if approval_id
        else None
    )
    payload = approval.result if approval and approval.status == "completed" else None
    if (
        not value
        or not payload
        or value.get("status") != "published"
        or payload.get("project_id") != str(run.project_id)
    ):
        return None
    urn = post_urn(value.get("urn"), value.get("url"))
    if urn is None:
        return None
    published = value.get("published_at")
    when = (
        datetime.fromisoformat(published)
        if isinstance(published, str)
        # Posts published before Zernio's time was kept count from the publish run's end.
        else run.finished_at or run.started_at or run.created_at
    )
    account = payload["account"]
    return {
        "urn": urn,
        "account_id": account["id"],
        "account_urn": account["urn"],
        "account_kind": account["kind"],
        "account_name": account["name"],
        "url": value.get("url"),
        "published_at": when.astimezone(UTC),
        "publish_run_id": run.id,
        "draft_path": payload.get("path"),
        "draft_post_id": payload.get("post_id"),
        "excerpt": excerpt(payload.get("text") or ""),
    }


def connected(accounts: list[dict[str, Any]], post: dict[str, Any]) -> bool:
    """Whether the post's account is still in the project's own Zernio profile."""

    return any(
        item.get("id") == post["account_id"]
        and item.get("urn") == post["account_urn"]
        and item.get("active")
        for item in accounts
    )


# Postgres: the record listening keeps.


async def track(db, *, project_id: UUID, post: dict[str, Any], conn=None) -> None:
    """Keep one post. A post found on the page first gains its publish run once Tin sees it."""

    await (conn or db.pool).execute(
        """
        INSERT INTO linkedin_posts (project_id, urn, account_id, account_urn, account_kind,
            account_name, url, published_at, publish_run_id, draft_path, draft_post_id, excerpt)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        ON CONFLICT (project_id, urn) DO UPDATE SET
            url = COALESCE(linkedin_posts.url, EXCLUDED.url),
            publish_run_id = COALESCE(linkedin_posts.publish_run_id, EXCLUDED.publish_run_id),
            draft_path = COALESCE(linkedin_posts.draft_path, EXCLUDED.draft_path),
            draft_post_id = COALESCE(linkedin_posts.draft_post_id, EXCLUDED.draft_post_id)
        """,
        project_id,
        post["urn"],
        post["account_id"],
        post["account_urn"],
        post["account_kind"],
        post["account_name"][:200],
        post.get("url"),
        post["published_at"],
        post.get("publish_run_id"),
        post.get("draft_path"),
        post.get("draft_post_id"),
        post.get("excerpt") or "",
    )


async def tracked(db, project_id: UUID, urn: str, *, conn=None) -> dict[str, Any] | None:
    row = await (conn or db.pool).fetchrow(
        "SELECT * FROM linkedin_posts WHERE project_id = $1 AND urn = $2", project_id, urn
    )
    return dict(row) if row else None


KEEP_READING = """
    INSERT INTO linkedin_post_readings (project_id, urn, checkpoint, metrics)
    VALUES ($1, $2, $3, $4::jsonb)
    ON CONFLICT (project_id, urn, checkpoint) DO NOTHING
"""
REPLACE_READING = """
    INSERT INTO linkedin_post_readings (project_id, urn, checkpoint, metrics)
    VALUES ($1, $2, $3, $4::jsonb)
    ON CONFLICT (project_id, urn, checkpoint)
    DO UPDATE SET metrics = EXCLUDED.metrics, taken_at = now()
"""


async def record_reading(
    db, *, project_id: UUID, urn: str, checkpoint: str, values: dict, replace: bool = False
) -> None:
    """A checkpoint keeps its first reading; a daily one keeps that day's latest."""

    await db.pool.execute(
        REPLACE_READING if replace else KEEP_READING,
        project_id,
        urn,
        checkpoint,
        json.dumps(values),
    )


async def mark_removed(db, *, project_id: UUID, urn: str) -> None:
    await db.pool.execute(
        """UPDATE linkedin_posts SET removed_at = COALESCE(removed_at, now())
           WHERE project_id = $1 AND urn = $2""",
        project_id,
        urn,
    )


async def record_followers(
    db, *, project_id: UUID, account_urn: str, day: date, followers: int
) -> None:
    await db.pool.execute(
        """
        INSERT INTO linkedin_follower_counts (project_id, account_urn, day, followers)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (project_id, account_urn, day) DO UPDATE SET followers = EXCLUDED.followers
        """,
        project_id,
        account_urn,
        day,
        followers,
    )


async def due(db, project_id: UUID, *, now: datetime) -> list[dict[str, Any]]:
    """The posts the daily run reads: published in the window and still on LinkedIn."""

    rows = await db.pool.fetch(
        """SELECT * FROM linkedin_posts
           WHERE project_id = $1 AND removed_at IS NULL AND published_at > $2
           ORDER BY published_at DESC""",
        project_id,
        now - timedelta(days=WINDOW_DAYS + 1),
    )
    return [dict(row) for row in rows]


async def untracked_publications(db, project_id: UUID, *, now: datetime) -> list[UUID]:
    """Publish runs from the window whose post listening doesn't know yet."""

    return [
        row["id"]
        for row in await db.pool.fetch(
            """SELECT r.id FROM workflow_runs r
               WHERE r.project_id = $1 AND r.executor = 'social.linkedin_publish'
                 AND r.status = 'succeeded' AND r.finished_at > $2
                 AND NOT EXISTS (SELECT 1 FROM linkedin_posts p
                                 WHERE p.project_id = r.project_id AND p.publish_run_id = r.id)
               ORDER BY r.finished_at""",
            project_id,
            now - timedelta(days=WINDOW_DAYS + 1),
        )
    ]


async def snapshot(db, project_id: UUID, *, now: datetime, conn=None) -> dict[str, Any]:
    """What the results files show, read in one place."""

    source = conn or db.pool
    posts = [
        dict(row)
        for row in await source.fetch(
            """SELECT * FROM linkedin_posts WHERE project_id = $1 AND published_at > $2
               ORDER BY published_at DESC, urn LIMIT $3""",
            project_id,
            now - timedelta(days=LISTED_DAYS),
            MAX_LISTED,
        )
    ]
    readings = await source.fetch(
        """SELECT urn, checkpoint, taken_at, metrics FROM linkedin_post_readings
           WHERE project_id = $1 AND urn = ANY($2::text[])""",
        project_id,
        [post["urn"] for post in posts],
    )
    by_post: dict[str, dict[str, dict]] = {}
    for row in readings:
        values = row["metrics"]
        by_post.setdefault(row["urn"], {})[row["checkpoint"]] = {
            "taken_at": row["taken_at"],
            **(json.loads(values) if isinstance(values, str) else dict(values)),
        }
    for post in posts:
        post["readings"] = by_post.get(post["urn"], {})
    followers = [
        dict(row)
        for row in await source.fetch(
            """SELECT account_urn, day, followers FROM linkedin_follower_counts
               WHERE project_id = $1 AND day >= $2 ORDER BY day""",
            project_id,
            (now - timedelta(days=WINDOW_DAYS)).date(),
        )
    ]
    return {"posts": posts, "followers": followers}


# The files.


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _reading(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "taken_at": _iso(values["taken_at"]),
        **{name: values.get(name, 0) for name in METRICS},
        "engagement_rate": values.get("engagement_rate"),
    }


def _latest(readings: dict[str, dict]) -> tuple[str, dict] | None:
    if not readings:
        return None
    return max(readings.items(), key=lambda item: item[1]["taken_at"])


def _followers(snapshot: dict, accounts: list[dict]) -> list[dict[str, Any]]:
    series: dict[str, list[dict]] = {}
    for row in snapshot["followers"]:
        series.setdefault(row["account_urn"], []).append(row)
    result = []
    for account in accounts:
        rows = series.get(account["urn"], [])
        latest = rows[-1] if rows else None
        first = rows[0] if len(rows) > 1 else None
        result.append(
            {
                "urn": account["urn"],
                "kind": account["kind"],
                "name": account["name"],
                "followers": latest["followers"] if latest else None,
                "counted_on": latest["day"].isoformat() if latest else None,
                "change": latest["followers"] - first["followers"] if first else None,
                "change_since": first["day"].isoformat() if first else None,
            }
        )
    return result


def results(snapshot: dict, accounts: list[dict], *, now: datetime) -> dict[str, Any]:
    """results.json: the posts, their readings and the accounts' followers."""

    posts = []
    for post in snapshot["posts"]:
        readings = post["readings"]
        latest = _latest(readings)
        posts.append(
            {
                "urn": post["urn"],
                "url": post["url"],
                "account": {
                    "urn": post["account_urn"],
                    "kind": post["account_kind"],
                    "name": post["account_name"],
                },
                "published_at": _iso(post["published_at"]),
                "source": "tin" if post["publish_run_id"] else "linkedin",
                "draft": {"path": post["draft_path"], "post_id": post["draft_post_id"]}
                if post["draft_path"]
                else None,
                "excerpt": post["excerpt"],
                "removed_at": _iso(post["removed_at"]) if post["removed_at"] else None,
                "readings": {
                    label: _reading(readings[label]) for label in SHOWN if label in readings
                },
                "latest": {"checkpoint": latest[0], **_reading(latest[1])} if latest else None,
            }
        )
    return {
        "schema_version": SCHEMA,
        "updated_at": _iso(now),
        "window_days": WINDOW_DAYS,
        "accounts": _followers(snapshot, accounts),
        "posts": posts,
    }


def _day(value: datetime | str) -> str:
    """14 Oct 2026, from a time or an ISO date or time."""

    when = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    return f"{when.day} {when:%b %Y}"


def _cell(value) -> str:
    return "–" if value is None else f"{value:,}"


def _when(label: str) -> str:
    if label in LABELS:
        return LABELS[label]
    if label.startswith("d"):
        days = int(label[1:])
        return f"{days} day{'s' if days != 1 else ''}"
    return label


def markdown(document: dict[str, Any]) -> str:
    """RESULTS.md, written from results.json so both always agree."""

    updated = datetime.fromisoformat(document["updated_at"].replace("Z", "+00:00"))
    lines = [
        "# LinkedIn results",
        "",
        f"Updated {_day(updated)}, {updated:%H:%M} UTC. {INTRO}",
    ]
    accounts = [item for item in document["accounts"] if item["followers"] is not None]
    if accounts:
        lines += ["", "## Followers", ""]
        for item in accounts:
            kind = "page" if item["kind"] == "organization" else "profile"
            change = ""
            if item["change"] is not None:
                change = (
                    f", {'up' if item['change'] >= 0 else 'down'} {abs(item['change']):,} "
                    f"since {_day(item['change_since'])}"
                )
            lines.append(f"- {item['name']} ({kind}): {item['followers']:,}{change}")
    lines += ["", "## Posts", ""]
    if not document["posts"]:
        lines.append(
            "No posts yet. Posts Tin publishes appear here an hour after posting, and page "
            "posts made on LinkedIn appear after the next daily run."
        )
    for post in document["posts"]:
        account = post["account"]
        kind = "page" if account["kind"] == "organization" else "profile"
        lines += [f"### {_day(post['published_at'])} · {account['name']}", ""]
        if post["excerpt"]:
            lines += [f"> {post['excerpt']}", ""]
        where = []
        if post["url"]:
            where.append(f"[View on LinkedIn]({post['url']})")
        if post["draft"]:
            where.append(
                f"From `{post['draft']['path']}`, post {post['draft']['post_id'] or 'unknown'}"
            )
        elif post["source"] == "linkedin":
            where.append(f"Posted on LinkedIn by the {kind}")
        if where:
            lines += [" · ".join(where), ""]
        if post["removed_at"]:
            lines += [
                f"Removed from LinkedIn by {_day(post['removed_at'])}, so Tin stopped reading it.",
                "",
            ]
        rows = [(label, values) for label, values in post["readings"].items()]
        latest = post["latest"]
        if latest and latest["checkpoint"] not in post["readings"]:
            rows.append((f"latest:{latest['checkpoint']}", latest))
        if not rows:
            if not post["removed_at"]:
                lines += ["No reading yet.", ""]
            continue
        extra = "Clicks" if account["kind"] == "organization" else "Saves"
        lines += [
            "| After | Impressions | Reach | Reactions | Comments | Shares | "
            f"{extra} | Engagement |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for label, values in rows:
            name = (
                f"{_when(label.split(':', 1)[1])} (latest)"
                if label.startswith("latest:")
                else _when(label)
            )
            rate = values["engagement_rate"]
            lines.append(
                f"| {name} | {_cell(values['impressions'])} | {_cell(values['reach'])} | "
                f"{_cell(values['reactions'])} | {_cell(values['comments'])} | "
                f"{_cell(values['shares'])} | "
                f"{_cell(values['clicks' if extra == 'Clicks' else 'saves'])} | "
                f"{'–' if rate is None else f'{rate:.2f}%'} |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render(snapshot: dict, accounts: list[dict], *, now: datetime) -> dict[str, str]:
    document = results(snapshot, accounts, now=now)
    return {
        RESULTS_JSON: json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        RESULTS_MD: markdown(document),
    }
