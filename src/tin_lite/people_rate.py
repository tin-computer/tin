"""Rate everyone in People who did something and has no current rating.

`people.rate` runs the one ICP rater (`people_rating`, the same call RB2B and the LinkedIn reads
use) over the project's people who visited, reacted, commented or viewed a profile and have no
rating under the current ICP file. It's how a project's earlier visitors get rated when it
starts using the rater, and how everyone is re-rated after the ICP file changes. Imported
connections with nothing but a `connected` event are never picked up. A member's own rating is
never replaced.

One run rates up to `people_icp.MAX_SCORED` people; a larger backlog takes a few runs. The run
lists whom it rated in people/ratings.md. Only counts pass through Temporal.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from tin_lite import people_icp
from tin_lite.domain import PEOPLE_RATE_WORKFLOW_NAME

KEY = PEOPLE_RATE_WORKFLOW_NAME
WORKFLOW_ID = UUID("4c7e2a91-d35b-4f08-a6e2-8b1f9c03d7a5")
INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"project_id": {"type": "string", "format": "uuid"}},
    "required": ["project_id"],
}
PATH = "people/ratings.md"
PATHS = {"ratings.md": PATH}
LIMITS = {"ratings.md": 400_000}
# People listed in the file, best fit first; the rest are counted.
LISTED = 400
# Ratings these runs make per project per day: a runaway guard well above a backfill.
RATED_PER_DAY = 1500


def _cell(value: Any) -> str:
    return " ".join(str(value or "").split()).replace("|", "/")


def report(people: list[dict], *, rated: int, left: int) -> str:
    """The run's ratings, best fit first, with each reason."""

    lines = ["# People rated against your ICP", ""]
    if not people:
        lines.append("Nobody needed a rating this time.")
    else:
        lines += [
            f"Tin rated {rated} {'person' if rated == 1 else 'people'} who visited your site or "
            "engaged on LinkedIn and had no rating under your current ICP file.",
            "",
            "| Name | Headline | Fit | Intent | Why |",
            "| --- | --- | --- | --- | --- |",
        ]
        for person in people[:LISTED]:
            fit = person.get("fit")
            label = f"{fit} · {people_icp.RATINGS[fit]}" if fit in people_icp.RATINGS else "—"
            lines.append(
                f"| {_cell(person.get('name'))} | {_cell(person.get('headline'))} | {label} | "
                f"{_cell(person.get('intent')) or '—'} | {_cell(person.get('reason'))} |"
            )
        if len(people) > LISTED:
            lines += ["", f"And {len(people) - LISTED} more, in People."]
    if left:
        lines += ["", f"{left} more still need a rating; the next run continues with them."]
    return "\n".join(lines) + "\n"


def summary(
    *, rated: int, good: int, left: int, available: bool, has_icp: bool, held: str = ""
) -> str:
    if held == "running":
        return "Another rating run for this project is still going, so this one rated nobody."
    if held == "daily":
        return "Tin has rated as many people as it does in a day here; start it again tomorrow."
    if not available:
        return "Rating isn't available on this deployment, so nobody was rated."
    if not has_icp:
        return "Add strategy/ICP.md so Tin can rate people against it."
    if not rated:
        return "Everyone who did something already has a current rating."
    said = f"Rated {rated} {'person' if rated == 1 else 'people'}"
    said += f", {good} a good or strong fit" if good else ", none a good or strong fit"
    return said + (f"; {left} still to rate." if left else ".")
