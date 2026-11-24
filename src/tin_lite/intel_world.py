"""`intel.world`: the shared world feed Tin's daily intelligence reports read.

Once a day, on the operator's host project (TIN_LITE_INTEL_HOST_PROJECT_ID), code pulls about
1,500 items from about 60 world sources, an Antigravity judge (`intel.world_judge`) writes the
day's stories into Postgres (global, `project_id` NULL) and a brief, and Tin publishes the
stories to the host project's Files and posts the summary to Tin's Slack. Client runs
(`intel.daily`) read the stories. See docs/daily-intelligence.md.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from tin_lite.intel_judge import WORLD_JUDGE_KEY
from tin_lite.workflow_diagrams import DiagramEdge, DiagramNode, WorkflowDiagram

KEY = "intel.world"
WORKFLOW_ID = UUID("fd012c10-407d-4686-94c6-339b2a43fdda")
JUDGE_KEY = WORLD_JUDGE_KEY
JUDGE_WORKFLOW_ID = UUID("5216b8e8-923b-46d5-9149-238510cb663f")
POLICY = {"version": "intel-v1"}
# The world source config, published beside the definition and read at the run's revision.
SOURCES_PATH = "workflows/intel.world/sources.json"
SCHEDULE_TIME = "06:00"
# A world day with this many stories posts even when its judge stopped early.
PARTIAL_MIN_STORIES = 10
WINDOW_HOURS = 24
CONTINUE_HOURS = 36

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"project_id": {"type": "string", "format": "uuid"}},
    "required": ["project_id"],
}


def stories_path(day: str) -> str:
    return f"intel/world/{day}/STORIES.md"


PRESENTATION = WorkflowDiagram(
    direction="TD",
    nodes=(
        DiagramNode("sources", "surface", "World sources", "about 60: wires, Google News, HN"),
        DiagramNode("fetch", "step", "Pull the last day", "paced, every source's own rank"),
        DiagramNode("snapshot", "store", "The day's items", "Postgres, a snapshot for the judge"),
        DiagramNode("judge", "step", "Write the stories", "Antigravity on Gemini 4 Neon"),
        DiagramNode("stories", "store", "World stories", "shared by every client's report"),
        DiagramNode("brief", "step", "Check the brief", "links to today's items only"),
        DiagramNode("files", "receipt", "Stories in Files", "STORIES.md in intel/world, by day"),
        DiagramNode("slack", "surface", "Post to Slack", "Tin's own channel, once a day"),
    ),
    edges=(
        DiagramEdge("sources", "fetch"),
        DiagramEdge("fetch", "snapshot"),
        DiagramEdge("snapshot", "judge", "call", "a child run"),
        DiagramEdge("judge", "stories"),
        DiagramEdge("judge", "brief"),
        DiagramEdge("stories", "files"),
        DiagramEdge("brief", "slack", label="or Tin's own from the top stories"),
    ),
)

JUDGE_PRESENTATION = WorkflowDiagram(
    direction="TD",
    nodes=(
        DiagramNode("day", "store", "Today's items", "read-only, searched with intel"),
        DiagramNode("prior", "store", "Earlier stories", "the last seven days"),
        DiagramNode("read", "step", "Read the fronts", "each source's own rank, search, read"),
        DiagramNode("stories", "step", "Record stories", "intel story put, checked by Tin"),
        DiagramNode("brief", "step", "Send the brief", "intel brief put, five to eight stories"),
        DiagramNode("notes", "receipt", "NOTES.md", "what dominated, odd data, tomorrow"),
    ),
    edges=(
        DiagramEdge("day", "read"),
        DiagramEdge("prior", "read"),
        DiagramEdge("read", "stories"),
        DiagramEdge("stories", "brief"),
        DiagramEdge("brief", "notes"),
    ),
)


def render_stories(day: str, stories: list[dict], *, items: int, sources: int) -> str:
    """STORIES.md: the day's world stories by section, high first, with their links."""
    from tin_lite.intel_brief import WORLD_GROUPS, group_of

    titles = {
        "world": "World",
        "tech_ai": "Tech and AI",
        "business": "Business and markets",
        "science_health": "Science and health",
        "culture": "Culture and sport",
    }
    order = {"high": 0, "medium": 1, "low": 2}
    grouped: dict[str, list[dict]] = {name: [] for name in WORLD_GROUPS}
    for story in stories:
        grouped[group_of(story.get("tags") or []) or "world"].append(story)
    high = sum(1 for s in stories if s.get("relevance") == "high")
    lines = [
        f"# World stories, {day}",
        "",
        f"{len(stories)} stories ({high} high) from {items:,} items and {sources} sources.",
        "Stories marked sensitive are never a hook for a brand.",
    ]
    for name, members in grouped.items():
        if not members:
            continue
        lines += ["", f"## {titles[name]}", ""]
        for story in sorted(
            members, key=lambda s: (order.get(s.get("relevance") or "", 3), s["key"])
        ):
            flags = [story.get("relevance") or "unrated"]
            if story.get("sensitive"):
                flags.append("sensitive")
            lines.append(f"### {story['title']}")
            lines.append("")
            lines.append(f"`{story['sid']}` · {', '.join(flags)} · {', '.join(story['tags'])}")
            if story.get("continues"):
                lines.append(f"Continues `{story['continues']}`.")
            lines.append("")
            lines.append(story["summary"])
            lines.append("")
            for entry in (story.get("items") or [])[:8]:
                label = (entry.get("title") or entry["url"]).replace("[", "(").replace("]", ")")
                lines.append(f"- [{label}]({entry['url']}) ({entry['source']})")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
