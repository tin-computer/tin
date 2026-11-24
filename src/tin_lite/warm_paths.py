"""Warm paths to a visitor: who on the project's team already knows them, or someone there.

Two tiers, both from the LinkedIn networks of the project's own members, never another
project's:

1. Free, from People: is the visitor a member's first-degree connection, and which of the
   members' connections work at the visitor's company (their `company` value, read by
   `social.linkedin_connections`)?
2. One LinkedIn read, for a good or strong fit only, when tier 1 found no direct connection: a
   search of a member's second-degree network for the visitor's name, through their saved
   session in the existing search sandbox. The visitor's result card names the mutual
   connections. It's paced by a daily cap per project, well above a day's good fits.

The result stays in the read's receipt; Temporal carries the run ID only.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote
from uuid import UUID

PROVIDER = "network.linkedin"
# Connections at the visitor's company named in the line; the rest are counted.
NAMED = 2
# Second-degree searches per project per day: a runaway guard, not a pacing estimate.
SEARCHES_PER_DAY = 40
SEARCH_SECONDS = 80

SUFFIXES = {
    "inc", "incorporated", "llc", "ltd", "limited", "corp", "corporation", "co", "company",
    "plc", "gmbh", "ag", "sa", "sas", "bv", "nv", "srl", "spa", "pty", "lp", "llp", "the",
    "group", "holdings", "holding",
}  # fmt: skip


def company_key(name: str | None) -> str:
    """A company name for matching: case, accents, punctuation and legal suffixes dropped."""

    if not name:
        return ""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    text = text.replace("&", " and ")
    words = re.findall(r"[a-z0-9]+", text)
    while words and words[-1] in SUFFIXES:
        words.pop()
    while words and words[0] == "the":
        words.pop(0)
    return " ".join(words)


@dataclass
class Known:
    """What tier 1 found: members connected to the visitor, and connections at their company."""

    connected: list[str] = field(default_factory=list)
    at_company: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"connected": self.connected, "at_company": self.at_company}


async def known(conn, project_id: UUID, record_id: UUID, company: str | None) -> Known:
    """Tier 1: the members connected to the visitor, and the members' connections whose
    current company is the visitor's, most recently read first."""

    found = Known()
    rows = await conn.fetch(
        """SELECT member FROM record_relationships
           WHERE project_id = $1 AND record_id = $2 AND state = 'connected'
           ORDER BY set_at""",
        project_id,
        record_id,
    )
    found.connected = [row["member"] for row in rows]
    found.at_company = await at_company(conn, project_id, company, besides=record_id)
    return found


async def at_company(
    conn, project_id: UUID, company: str | None, *, besides: UUID | None = None
) -> list[dict[str, Any]]:
    """The members' first-degree connections whose current company is this one, most recently
    read first; `besides` leaves the visitor themself out."""

    wanted = company_key(company)
    if not wanted:
        return []
    return (await _by_company(conn, project_id, besides=besides)).get(wanted, [])


async def _by_company(
    conn, project_id: UUID, *, besides: UUID | None = None
) -> dict[str, list[dict[str, Any]]]:
    """The members' first-degree connections with a current company, by its matching key, most
    recently read first: one read for any number of companies."""

    rows = await conn.fetch(
        """SELECT r.id, rel.member, c.value_text AS company, n.value_text AS name,
                  p.value_text AS position
           FROM record_relationships rel
           JOIN records r ON r.project_id = rel.project_id AND r.id = rel.record_id
           JOIN record_values c ON c.record_id = r.id AND c.field = 'company'
           LEFT JOIN record_values n ON n.record_id = r.id AND n.field = 'name'
           LEFT JOIN record_values p ON p.record_id = r.id AND p.field = 'position'
           WHERE rel.project_id = $1 AND rel.state = 'connected'
             AND r.id IS DISTINCT FROM $2
             AND r.merged_into IS NULL AND c.value_text IS NOT NULL
           ORDER BY c.set_at DESC""",
        project_id,
        besides,
    )
    seen: set[UUID] = set()
    found: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = company_key(row["company"])
        if row["id"] in seen or not row["name"] or not key:
            continue
        seen.add(row["id"])
        found.setdefault(key, []).append(
            {
                "record_id": str(row["id"]),
                "member": row["member"],
                "name": row["name"],
                "position": row["position"] or "",
            }
        )
    return found


async def known_many(
    conn, project_id: UUID, people: list[tuple[UUID, str | None]]
) -> dict[UUID, Known]:
    """Tier 1 for many people at once, as `known` finds it for each: `people` pairs a record
    with its company. Two reads, however many people."""

    if not people:
        return {}
    ids = [record_id for record_id, _ in people]
    connected: dict[UUID, list[str]] = {}
    for row in await conn.fetch(
        """SELECT record_id, member FROM record_relationships
           WHERE project_id = $1 AND record_id = ANY($2::uuid[]) AND state = 'connected'
           ORDER BY set_at""",
        project_id,
        ids,
    ):
        connected.setdefault(row["record_id"], []).append(row["member"])
    companies = await _by_company(conn, project_id)
    found: dict[UUID, Known] = {}
    for record_id, company in people:
        there = companies.get(company_key(company), []) if company_key(company) else []
        found[record_id] = Known(
            connected=connected.get(record_id, []),
            at_company=[p for p in there if p["record_id"] != str(record_id)],
        )
    return found


def _slug(url: str | None) -> str:
    if not url:
        return ""
    match = re.search(r"linkedin\.com/in/([^/?#]+)", url)
    return unquote(match.group(1)).lower() if match else ""


def lead_in(results: list[dict], *, profile_url: str | None, member: str | None, name: str,
            company: str | None) -> dict | None:  # fmt: skip
    """The visitor's card among a second-degree search's results: the same profile, or the one
    card with exactly their name whose headline names their company. Nothing else counts."""

    wanted = {value.lower() for value in (_slug(profile_url), member or "") if value}
    for person in results:
        if _slug(person.get("profile_url")) in wanted:
            return person
    key = company_key(company)
    if not key:
        return None
    wanted_name = " ".join(name.split()).lower()
    same = [
        person
        for person in results
        if " ".join((person.get("name") or "").split()).lower() == wanted_name
        and re.search(rf"\b{re.escape(key)}\b", company_key(person.get("headline")))
    ]
    return same[0] if len(same) == 1 else None


def line(
    *,
    known: dict[str, Any] | None,
    second: dict[str, Any] | None,
    names: dict[str, str],
    company: str | None,
    you: str | None = None,
) -> str | None:
    """The one 🤝 line for Slack and People, or None when there's no path. `names` maps members
    to first names; members without one are "Your team". `you` is the member reading it in
    People, who reads as "You"."""

    def who(members: list[str]) -> tuple[str, bool]:
        """Who, and whether they take a plural verb ("are", "know")."""

        shown = [
            "You" if member == you else names[member]
            for member in members
            if member == you or names.get(member)
        ]
        shown = sorted(dict.fromkeys(shown), key=lambda name: name != "You")
        if not shown:
            return "Your team", False
        text = shown[0] if len(shown) == 1 else f"{', '.join(shown[:-1])} and {shown[-1]}"
        return text, len(shown) > 1 or shown[0] == "You"

    known = known or {}
    if known.get("connected"):
        members, plural = who(known["connected"])
        return f"🤝 {members} {'are' if plural else 'is'} already connected"
    if second and second.get("mutual_count"):
        shown = [n for n in second.get("mutual_names") or [] if n][:NAMED]
        rest = int(second["mutual_count"]) - len(shown)
        via = ", ".join(shown) if shown else f"{second['mutual_count']} mutual connections"
        if shown and rest > 0:
            via += f" + {rest} other{'s' if rest != 1 else ''}"
        return f"🤝 2nd degree via {via}"
    people = known.get("at_company") or []
    if people:
        members = list(dict.fromkeys(p["member"] for p in people))
        shown = [
            p["name"] + (f" ({p['position']})" if p.get("position") else "") for p in people[:NAMED]
        ]
        rest = len(people) - len(shown)
        tail = f" + {rest} more" if rest > 0 else ""
        at = f" at {company}" if company else ""
        team, plural = who(members)
        verb = "know" if plural or team == "Your team" else "knows"
        return f"🤝 {team} {verb} {', '.join(shown)}{tail}{at}"
    return None
