"""Who viewed a founder's LinkedIn profile, kept as People records.

`social.linkedin_profile_viewers` reads, with the member's saved LinkedIn session, the list on
their profile-views page, newest first, and LinkedIn's overview of the last 90 days. Every
named viewer becomes a People record (matched on their profile ID or public slug) with their
name, headline and whether they're already a connection, and a `viewed` event. The same model
step as `social.linkedin_engagers` scores them against the project's ICP file; viewers who fit
and aren't connected join the member's connection requests in Decisions. The list is rendered
to project Files as a CSV and a short summary.

LinkedIn says when someone looked as "3d ago" or "2w ago", so a view's date is as exact as
that: one view per person within that span counts once, however many runs see it.

Only counts pass through Temporal. Names, headlines and scores stay in Postgres and in the
rendered files; the next page's request stays in its read's receipt.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from tin_lite import linkedin_engagers as engagers
from tin_lite import people_records as records
from tin_lite.domain import LINKEDIN_PROFILE_VIEWERS_WORKFLOW_NAME
from tin_lite.people_records import Value

KEY = LINKEDIN_PROFILE_VIEWERS_WORKFLOW_NAME
WORKFLOW_ID = UUID("8a3f6c1d-2e95-4b7a-a0d4-6c1e9f3b52a7")
# Reads per run, ten pages of ten viewers each. A daily run stops at the first read that adds
# nobody new; the first run reads back as far as this allows.
MAX_READS = 3
PAGES_PER_READ = 10
# Once a member's viewers were read before, each read takes two pages: a day's views fit, and
# the run reads on while it finds new ones.
LATER_PAGES = 2
# Slug-only viewers who fit looked up for their profile ID per run, so they can be invited.
IDS_PER_RUN = 20
CSV_PATH = "people/linkedin-profile-viewers.csv"
SUMMARY_PATH = "people/linkedin-profile-viewers.md"
PATHS = {
    "linkedin-profile-viewers.csv": CSV_PATH,
    "linkedin-profile-viewers.md": SUMMARY_PATH,
}
LIMITS = {"linkedin-profile-viewers.csv": 2_000_000, "linkedin-profile-viewers.md": 64_000}
INPUT_SCHEMA: dict[str, Any] = engagers.INPUT_SCHEMA
AGE = re.compile(r"(\d{1,3})(m|min|mins|h|hr|hrs|d|w|wk|mo|y|yr|yrs)")
# How long one unit of LinkedIn's "… ago" is, and how far either side of the date a view may
# really be: "3d ago" is within a day, "2w ago" within half a week.
UNITS = {
    "m": (timedelta(minutes=1), 1),
    "min": (timedelta(minutes=1), 1),
    "mins": (timedelta(minutes=1), 1),
    "h": (timedelta(hours=1), 1),
    "hr": (timedelta(hours=1), 1),
    "hrs": (timedelta(hours=1), 1),
    "d": (timedelta(days=1), 1),
    "w": (timedelta(weeks=1), 4),
    "wk": (timedelta(weeks=1), 4),
    "mo": (timedelta(days=30), 15),
    "y": (timedelta(days=365), 182),
    "yr": (timedelta(days=365), 182),
    "yrs": (timedelta(days=365), 182),
}


def viewed_when(viewer: dict, read_at: datetime) -> tuple[datetime, int]:
    """When a viewer looked, and how many days either side that may really be."""

    if viewer.get("viewed_at"):
        return datetime.fromisoformat(viewer["viewed_at"]), 1
    match = AGE.fullmatch(viewer.get("age") or "")
    if not match:
        return read_at, 1
    unit, window = UNITS[match[2]]
    return read_at - int(match[1]) * unit, window


def viewed_key(member: str, record_id: UUID, day: str) -> str:
    return f"linkedin:viewed:{member}:{record_id}:{day}"


async def record_viewers(
    conn, *, project_id: UUID, run_id: UUID, member: str, viewers: list[dict], read_at: datetime
) -> dict[str, int]:
    """Write a read's viewers as records, values and `viewed` events. Whether each is a
    connection is the run's starter's own relationship: it's their LinkedIn Tin reads."""

    counts = {"people": 0, "new": 0, "forgotten": 0}
    for viewer in viewers:
        identifiers = {
            kind: viewer[field]
            for kind, field in (("linkedin_member", "member"), ("linkedin_slug", "slug"))
            if viewer.get(field)
        }
        record_id = await records.upsert_person(
            conn, project_id, identifiers, source=KEY, run_id=run_id
        )
        if record_id is None:
            counts["forgotten"] += 1
            continue
        values = [Value("name", viewer["name"] or None, "")]
        values.append(
            Value(
                "headline",
                viewer["headline"] or None,
                "" if viewer["headline"] else "LinkedIn showed none",
            )
        )
        await records.set_values(
            conn, project_id, record_id, values, set_by_kind="run", set_by=str(run_id)
        )
        relationship = engagers.RELATIONSHIP.get(viewer.get("degree") or "")
        if relationship:
            await records.set_relationship(
                conn,
                project_id,
                record_id,
                member,
                relationship,
                set_by=str(run_id),
                reason=f"{viewer['degree']} on LinkedIn",
                keep_pending=True,
            )
        counts["people"] += 1
        when, window = viewed_when(viewer, read_at)
        target = await records.current(conn, project_id, record_id)
        # The same view, seen again by a later run (or in both the list and the overview).
        seen = await conn.fetchval(
            """SELECT EXISTS (
                   SELECT 1 FROM record_events e
                   JOIN records r ON r.id = e.record_id
                   JOIN workflow_runs w ON w.id = e.run_id
                   WHERE e.project_id = $1 AND e.kind = 'viewed'
                     AND COALESCE(r.merged_into, r.id) = $2
                     AND w.started_by_clerk_user_id = $3
                     AND e.occurred_at BETWEEN $4 AND $5)""",
            project_id,
            target,
            member,
            when - timedelta(days=window),
            when + timedelta(days=window),
        )
        if seen:
            continue
        counts["new"] += await records.add_event(
            conn,
            project_id,
            target,
            kind="viewed",
            event_key=viewed_key(member, target, f"{when:%Y-%m-%d}"),
            occurred_at=when,
            detail={"age": viewer.get("age"), "exact": bool(viewer.get("viewed_at"))},
            run_id=run_id,
        )
    return counts


async def need_ids(conn, project_id: UUID, member: str, *, limit: int) -> list[dict]:
    """Viewers of this member who fit an ICP and aren't connected, known only by their slug:
    sending them a request needs their profile ID."""

    rows = await conn.fetch(
        """SELECT r.id, i.value AS slug FROM records r
           JOIN record_identifiers i ON i.record_id = r.id AND i.kind = 'linkedin_slug'
           WHERE r.project_id = $1 AND r.merged_into IS NULL
             AND NOT EXISTS (SELECT 1 FROM record_identifiers m WHERE m.record_id = r.id
                             AND m.kind = 'linkedin_member')
             AND EXISTS (
                 SELECT 1 FROM record_events e
                 JOIN records x ON x.id = e.record_id
                 JOIN workflow_runs w ON w.id = e.run_id
                 WHERE e.project_id = $1 AND COALESCE(x.merged_into, x.id) = r.id
                   AND e.kind = 'viewed' AND w.started_by_clerk_user_id = $2)
             AND EXISTS (SELECT 1 FROM record_values v WHERE v.record_id = r.id
                         AND v.field = 'icp_fit' AND v.value_number >= $3)
             AND NOT EXISTS (SELECT 1 FROM record_relationships x WHERE x.record_id = r.id
                             AND x.member = $2 AND x.state <> 'none')
             AND NOT EXISTS (SELECT 1 FROM record_values v WHERE v.record_id = r.id
                             AND v.field = 'customer' AND v.value_text = 'yes')
           ORDER BY r.created_at, r.id
           LIMIT $4""",
        project_id,
        member,
        engagers.FIT_SHOWN,
        limit,
    )
    return [dict(row) for row in rows]


async def record_ids(
    conn, *, project_id: UUID, run_id: UUID, member: str, people: list[dict]
) -> int:
    """Add each looked-up profile ID to the person's record, with where the member stands."""

    found = 0
    for person in people:
        if not person.get("member"):
            continue
        record_id = await records.upsert_person(
            conn,
            project_id,
            {"linkedin_slug": person["slug"], "linkedin_member": person["member"]},
            source=KEY,
            run_id=run_id,
        )
        if record_id is None:
            continue
        found += 1
        state = {"connected": "connected", "invited": "pending", "none": "none"}.get(
            person["state"]
        )
        if state:
            await records.set_relationship(
                conn,
                project_id,
                record_id,
                member,
                state,
                set_by=str(run_id),
                reason="LinkedIn profile",
                keep_pending=state == "none",
            )
    return found


# The rendered list.

COLUMNS = (
    "Name",
    "Headline",
    "LinkedIn",
    "Connected",
    "ICP",
    "ICP fit",
    "Why",
    "Last viewed",
    "Views",
)


async def viewers(conn, project_id: UUID, member: str) -> list[dict]:
    """Everyone who viewed this member's profile, best fit first, with whether each is the
    member's own connection, when they last looked and how many times."""

    people = await records.people(conn, project_id)
    mine = await records.relationships(conn, project_id, member)
    views: dict[UUID, list[datetime]] = {}
    for row in await conn.fetch(
        """SELECT COALESCE(r.merged_into, r.id) AS person, e.occurred_at
           FROM record_events e
           JOIN records r ON r.id = e.record_id
           JOIN workflow_runs w ON w.id = e.run_id
           WHERE e.project_id = $1 AND e.kind = 'viewed' AND w.started_by_clerk_user_id = $2""",
        project_id,
        member,
    ):
        views.setdefault(row["person"], []).append(row["occurred_at"])
    listed = []
    for person in people:
        if person["id"] not in views:
            continue
        person["fields"].pop("linkedin_relationship", None)
        relationship = mine.get(person["id"])
        if relationship:
            person["fields"]["linkedin_relationship"] = {"text": relationship["state"]}
        seen = views[person["id"]]
        listed.append({**person, "last_viewed": max(seen), "views": len(seen)})

    def order(person):
        fit = (person["fields"].get("icp_fit") or {}).get("number")
        name = (person["fields"].get("name") or {}).get("text") or ""
        return (fit is None, -(fit or 0), -person["last_viewed"].timestamp(), name.casefold())

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
                engagers._cell(field(person, "name")),
                engagers._cell(field(person, "headline")),
                person["profile_url"],
                {"connected": "yes", "none": "no", "pending": "invited"}.get(
                    field(person, "linkedin_relationship"), ""
                ),
                engagers._cell(field(person, "icp")),
                f"{fit:.2f}" if fit != "" else "",
                engagers._cell(field(person, "icp_fit", "reason")),
                f"{person['last_viewed']:%Y-%m-%d}",
                person["views"],
            ]
        )
    return {CSV_PATH: buffer.getvalue(), SUMMARY_PATH: summary_markdown(people, notes, now=now)}


def summary_markdown(people: list[dict], notes: dict, *, now: datetime) -> str:
    def field(person, key, part="text"):
        return (person["fields"].get(key) or {}).get(part)

    new = [p for p in people if field(p, "linkedin_relationship") == "none"]
    fitting = [p for p in people if (field(p, "icp_fit", "number") or 0) >= engagers.FIT_SHOWN]
    fitting_new = [p for p in fitting if p in new]
    lines = [
        "# People who viewed your LinkedIn profile",
        "",
        f"Updated {now.astimezone(UTC):%-d %b %Y, %H:%M} UTC. "
        "Tin read who viewed your profile, newest first, as LinkedIn lists them. "
        f"The full list is in `{CSV_PATH}`, best fit first.",
        "",
        *([f"**{notes['notice']}**", ""] if notes.get("notice") else []),
        *(
            [
                "**LinkedIn changed its profile-views list, so Tin read only its overview this "
                "time. Tin is looking into it.**",
                "",
            ]
            if notes.get("format_changed")
            else []
        ),
        f"- {sentence(notes)}",
        f"- {len(people)} {'person' if len(people) == 1 else 'people'} in the list, "
        f"{len(new)} not connected to you yet.",
    ]
    if notes.get("total_views") is not None or notes.get("anonymous"):
        hidden = notes.get("anonymous", 0)
        lines.append(
            "- "
            + (
                f"LinkedIn counts {notes['total_views']} views in the last 90 days. "
                if notes.get("total_views") is not None
                else ""
            )
            + f"{hidden} of the views Tin read came from people browsing privately."
        )
    if notes.get("icp"):
        lines.append(
            f"- {len(fitting)} fit an ICP in `{notes['icp']}` at {engagers.FIT_SHOWN} or more, "
            f"{len(fitting_new)} of them not connected yet."
        )
    else:
        lines.append(
            "- No ICP file, so nobody is scored. Add `strategy/ICP.md`, one `##` section per "
            "profile, and the next run scores everyone."
        )
    if fitting_new:
        lines += ["", "## Best fits you're not connected to", ""]
        for person in fitting_new[:15]:
            lines.append(
                f"- [{field(person, 'name') or 'Someone'}]({person['profile_url']}), "
                f"{field(person, 'headline') or 'no headline'}: "
                f"{field(person, 'icp')} {field(person, 'icp_fit', 'number'):.2f}, "
                f"viewed {person['last_viewed']:%-d %b}. {field(person, 'icp_fit', 'reason')}"
            )
    lines += [
        "",
        "LinkedIn doesn't name people who browse privately; they're counted, not listed.",
        "",
    ]
    return "\n".join(lines)


def sentence(notes: dict) -> str:
    """The run's one line: what it found, then what waits for the founder."""

    new, people = notes.get("new", 0), notes.get("new_people", 0)
    text = f"{new} new profile view{'s' if new != 1 else ''}"
    if people:
        text += f", {people} {'people' if people != 1 else 'person'} new to Tin"
    text += "." + engagers.accepted_text(notes.get("accepted", 0))
    if notes.get("proposed"):
        count = notes["proposed"]
        text += (
            f" {count} people who fit and aren't connected wait"
            if count != 1
            else " 1 person who fits and isn't connected waits"
        ) + " in Decisions for your connection requests."
    return text
