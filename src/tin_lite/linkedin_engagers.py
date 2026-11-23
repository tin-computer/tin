"""Who engaged with a founder's own LinkedIn posts, kept as People records (#435).

`social.linkedin_engagers` reads the founder's own posts from the last 90 days with their saved
LinkedIn session, then each post's reactions. Every person who reacted becomes a People record
(matched on their LinkedIn profile ID) with their name, headline and whether they're already a
connection, and a `reacted` event per post. `people_icp` rates each person against the
project's ICP file. The list is rendered to project Files as a CSV and a short summary; the
`/people` view reads the same records.

Only identifiers and counts pass through Temporal. Names, headlines and scores stay in Postgres
and in the rendered files.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from tin_lite import people_records as records
from tin_lite.domain import LINKEDIN_ENGAGERS_WORKFLOW_NAME
from tin_lite.model_providers import ModelCapability, ModelRoute, ProviderName
from tin_lite.people_icp import FIT_SHOWN, RATINGS
from tin_lite.people_records import Value

KEY = LINKEDIN_ENGAGERS_WORKFLOW_NAME
WORKFLOW_ID = UUID("5d0c2f6e-8b41-4a37-9c1e-2e7f4b9a6d83")
WINDOW_DAYS = 90
MAX_POSTS = 60
CSV_PATH = "people/linkedin-engagers.csv"
SUMMARY_PATH = "people/linkedin-engagers.md"
PATHS = {"linkedin-engagers.csv": CSV_PATH, "linkedin-engagers.md": SUMMARY_PATH}
LIMITS = {"linkedin-engagers.csv": 2_000_000, "linkedin-engagers.md": 64_000}
RELATIONSHIP = {"1st": "connected", "2nd": "none", "3rd": "none"}
# The route runs of definitions from before people_icp pinned. Ratings follow people_icp now;
# the pin only says which worker serves the run.
LEGACY_ROUTE = ModelRoute(
    key="linkedin.engagers.icp",
    provider=ProviderName.OPENAI,
    model="gpt-6-luna",
    capabilities=frozenset({ModelCapability.TEXT, ModelCapability.JSON_SCHEMA}),
)

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"project_id": {"type": "string", "format": "uuid"}},
    "required": ["project_id"],
}


def reacted_key(thread_urn: str, member: str) -> str:
    return f"linkedin:reacted:{thread_urn}:{member}"


async def record_reactions(
    conn, *, project_id: UUID, run_id: UUID, member: str, post: dict, people: list[dict]
) -> dict[str, int]:
    """Write one page of a post's reactions as records, values and events. Whether each person
    is a connection is the run's starter's own relationship: it's their LinkedIn Tin reads."""

    counts = {"people": 0, "events": 0, "forgotten": 0}
    for person in people:
        record_id = await records.upsert_person(
            conn,
            project_id,
            {"linkedin_member": person["member"]},
            source=KEY,
            run_id=run_id,
        )
        if record_id is None:
            counts["forgotten"] += 1
            continue
        values = [
            Value("name", person["name"] or None, "" if person["name"] else "LinkedIn showed none"),
            Value(
                "headline",
                person["headline"] or None,
                "" if person["headline"] else "LinkedIn showed none",
            ),
        ]
        await records.set_values(
            conn, project_id, record_id, values, set_by_kind="run", set_by=str(run_id)
        )
        relationship = RELATIONSHIP.get(person.get("degree") or "")
        if relationship:
            await records.set_relationship(
                conn,
                project_id,
                record_id,
                member,
                relationship,
                set_by=str(run_id),
                reason=f"{person['degree']} on LinkedIn",
                keep_pending=True,
            )
        counts["people"] += 1
        counts["events"] += await records.add_event(
            conn,
            project_id,
            record_id,
            kind="reacted",
            event_key=reacted_key(post["thread_urn"], person["member"]),
            detail={
                "post": post["activity_urn"],
                "thread": post["thread_urn"],
                "posted_at": post["published_at"],
                "reaction": person.get("reaction"),
            },
            run_id=run_id,
        )
    return counts


# The rendered list.

COLUMNS = (
    "Name",
    "Headline",
    "LinkedIn",
    "Connected",
    "ICP",
    "ICP fit",
    "Why",
    "Reacted to posts from",
)


async def engagers(conn, project_id: UUID, member: str) -> list[dict]:
    """Everyone who engaged with the team's posts, best fit first, with whether each is the
    member's own connection."""

    people = await records.people(conn, project_id)
    mine = await records.relationships(conn, project_id, member)
    for person in people:
        relationship = mine.get(person["id"])
        person["fields"].pop("linkedin_relationship", None)
        if relationship:
            person["fields"]["linkedin_relationship"] = {"text": relationship["state"]}
    dates: dict[UUID, set[str]] = {}
    for row in await conn.fetch(
        """SELECT COALESCE(r.merged_into, r.id) AS person, e.detail->>'posted_at' AS posted_at
           FROM record_events e JOIN records r ON r.id = e.record_id
           WHERE e.project_id = $1 AND e.kind IN ('reacted', 'commented')""",
        project_id,
    ):
        if row["posted_at"]:
            dates.setdefault(row["person"], set()).add(row["posted_at"][:10])
    listed = [
        {**person, "posts": sorted(dates[person["id"]])}
        for person in people
        if person["id"] in dates
    ]

    def order(person):
        fit = (person["fields"].get("icp_fit") or {}).get("number")
        name = (person["fields"].get("name") or {}).get("text") or ""
        return (fit is None, -(fit or 0), name.casefold())

    return sorted(listed, key=order)


def render(people: list[dict], *, notes: dict, now: datetime) -> dict[str, str]:
    def field(person, key, part="text"):
        value = (person["fields"].get(key) or {}).get(part)
        return "" if value is None else value

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS)
    for person in people:
        fit = field(person, "icp_fit", "number")
        writer.writerow(
            [
                _cell(field(person, "name")),
                _cell(field(person, "headline")),
                person["profile_url"],
                {"connected": "yes", "none": "no"}.get(field(person, "linkedin_relationship"), ""),
                _cell(field(person, "icp")),
                rating_text(fit),
                _cell(field(person, "icp_fit", "reason")),
                ", ".join(person["posts"]),
            ]
        )
    return {CSV_PATH: buffer.getvalue(), SUMMARY_PATH: summary_markdown(people, notes, now=now)}


def rating_text(rating) -> str:
    """A rating as the list shows it: "4 · Contact this week"."""

    if rating in ("", None):
        return ""
    rating = int(rating)
    return f"{rating} · {RATINGS[rating]}" if rating in RATINGS else str(rating)


def _cell(value: str) -> str:
    # A spreadsheet runs a cell that starts with =, +, - or @ as a formula.
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value


def summary_markdown(people: list[dict], notes: dict, *, now: datetime) -> str:
    def field(person, key, part="text"):
        return (person["fields"].get(key) or {}).get(part)

    new = [p for p in people if field(p, "linkedin_relationship") == "none"]
    fitting = [p for p in people if (field(p, "icp_fit", "number") or 0) >= FIT_SHOWN]
    fitting_new = [p for p in fitting if p in new]
    lines = [
        "# People who engaged with your LinkedIn posts",
        "",
        f"Updated {now.astimezone(UTC):%-d %b %Y, %H:%M} UTC. "
        f"Tin read your posts from the last {WINDOW_DAYS} days and who reacted to them. "
        f"The full list is in `{CSV_PATH}`, best fit first.",
        "",
        *([f"**{notes['notice']}**", ""] if notes.get("notice") else []),
        f"- {sentence(notes)}",
        f"- {len(people)} {'person' if len(people) == 1 else 'people'} in the list, "
        f"{len(new)} not connected to you yet.",
    ]
    if notes.get("icp"):
        lines.append(
            f"- {len(fitting)} rated {FIT_SHOWN} or more against `{notes['icp']}`, "
            f"{len(fitting_new)} of them not connected yet."
        )
    else:
        lines.append(
            "- No ICP file, so nobody is rated. Add `strategy/ICP.md`, one `##` section per "
            "profile, and the next run rates everyone."
        )
    if fitting_new:
        lines += ["", "## Best fits you're not connected to", ""]
        for person in fitting_new[:15]:
            lines.append(
                f"- [{field(person, 'name') or 'Someone'}]({person['profile_url']}), "
                f"{field(person, 'headline') or 'no headline'}: "
                f"{field(person, 'icp')}, {rating_text(field(person, 'icp_fit', 'number'))}. "
                f"{field(person, 'icp_fit', 'reason')}"
            )
    lines += [
        "",
        "Comments aren't read yet. Tin reads reactions; comments need the next Tin for Chrome.",
        "",
    ]
    return "\n".join(lines)


def queued_text(notes: dict) -> str:
    """The connection requests a read queued, for its line."""

    if notes.get("queue_refused"):
        return " " + notes["queue_refused"]
    count = notes.get("queued", 0)
    if not count:
        return ""
    if count == 1:
        return " 1 connection request queued for someone who fits."
    return f" {count} connection requests queued for people who fit."


def accepted_text(count: int) -> str:
    """Who accepted the member's connection requests since the last read, for a run's line."""

    if not count:
        return ""
    if count == 1:
        return " 1 person accepted your connection request."
    return f" {count} people accepted your connection requests."


def sentence(notes: dict) -> str:
    posts, people = notes.get("posts", 0), notes.get("reactions", 0)
    text = (
        f"{posts} post{'s' if posts != 1 else ''} from the last {WINDOW_DAYS} days, "
        f"{people} reaction{'s' if people != 1 else ''} read"
    )
    if notes.get("new_people"):
        text += f", {notes['new_people']} new to Tin"
    if notes.get("unread"):
        text += f"; {notes['unread']} post{'s' if notes['unread'] != 1 else ''} couldn't be read"
    if notes.get("capped"):
        text += "; very large posts were read to their first 1,000 reactions"
    return text + "." + accepted_text(notes.get("accepted", 0)) + queued_text(notes)
