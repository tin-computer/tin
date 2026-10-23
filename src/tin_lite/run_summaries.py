"""A run's summary: the short, self-contained read of a result, kept on the run and posted to the
places a saved workflow names (Slack today).

A summary is format-neutral data, never Markdown or a provider's message format:

```json
{"version": 1,
 "headline": "Yesterday: 80 unique visitors, 2 clicked Start trial, 3 confirmed checkout.",
 "table": {"columns": ["", "Unique visitors", "Clicked Start trial"],
           "rows": [["Yesterday", "80", "2"], ["Last 7 days, daily average", "76.1", "3.4"]]},
 "sections": [{"heading": "Some visitors",
               "items": [{"text": "Michael Jones: started a trial after a retry.",
                          "url": "https://us.posthog.com/project/1/replay/01a1",
                          "label": "Replay"}]}]}
```

A workflow supports a summary when its result can produce one:

- a procedure whose output validator has a reader here (`SOURCES`), such as a visitor journal;
- a code workflow that declares `code.output.summary: true` and returns `summary` beside
  `path` and `content` (see `parse`).

Everything is bounded and plain text: a summary that breaks the shape is dropped, never fatal,
so it cannot fail a run that already published its result.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

VERSION = 1
HEADLINE = 300
TABLE_COLUMNS = 6
TABLE_ROWS = 10
CELL = 60
SECTIONS = 4
ITEMS = 8
ITEM = 300
HEADING = 60
LABEL = 24
URL = re.compile(r"^https://[^\s<>|]{1,500}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _text(value: Any, limit: int) -> str:
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return ""
    line = " ".join(_CONTROL.sub(" ", str(value)).split())
    if len(line) <= limit:
        return line
    return line[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"


def parse(value: Any) -> dict[str, Any] | None:
    """The summary in its bounded shape, or None when there is nothing usable."""
    if not isinstance(value, dict):
        return None
    headline = _text(value.get("headline"), HEADLINE)
    if not headline:
        return None
    table = None
    raw = value.get("table")
    if isinstance(raw, dict) and isinstance(raw.get("columns"), list):
        columns = [_text(c, CELL) for c in raw["columns"][:TABLE_COLUMNS]]
        rows = [
            [_text(cell, CELL) for cell in row[: len(columns)]]
            + [""] * (len(columns) - len(row[: len(columns)]))
            for row in (raw.get("rows") if isinstance(raw.get("rows"), list) else [])[:TABLE_ROWS]
            if isinstance(row, list)
        ]
        if len(columns) >= 2 and rows:
            table = {"columns": columns, "rows": rows}
    sections = []
    for section in (value.get("sections") if isinstance(value.get("sections"), list) else [])[
        :SECTIONS
    ]:
        if not isinstance(section, dict):
            continue
        heading = _text(section.get("heading"), HEADING)
        items = []
        for item in (section.get("items") if isinstance(section.get("items"), list) else [])[
            :ITEMS
        ]:
            if isinstance(item, str):
                item = {"text": item}
            if not isinstance(item, dict) or not (text := _text(item.get("text"), ITEM)):
                continue
            url = item.get("url") if isinstance(item.get("url"), str) else ""
            url = url if URL.match(url) else None
            label = (_text(item.get("label"), LABEL) or "Open") if url else None
            items.append({"text": text, "url": url, "label": label})
        if heading and items:
            sections.append({"heading": heading, "items": items})
    return {"version": VERSION, "headline": headline, "table": table, "sections": sections}


def _new_user(v) -> dict[str, Any]:
    """A new person: who, where they work, what they did; their LinkedIn, else the replay."""
    who = f"{v.name} ({v.email})" if v.name else v.email
    if v.work and v.work.headline:
        who += f", {v.work.headline}"
    if v.work and v.work.linkedin_url:
        return {"text": f"{who}: {v.line}", "url": v.work.linkedin_url, "label": "LinkedIn"}
    return {"text": f"{who}: {v.line}", "url": v.replay_url, "label": "Replay"}


def _visitor_journal(content: bytes | str, inputs: Any = None) -> dict[str, Any] | None:
    from tin_lite import visitor_journal

    outcome = visitor_journal.read(content, work=visitor_journal.work_allowed(inputs))
    if outcome.measured_nothing or outcome.status == "invalid configuration":
        return None
    brief = outcome.brief
    table = None
    sections = []
    if brief is not None:
        if brief.steps:

            def cell(value):
                return "not tracked" if value is None else f"{value:g}"

            table = {
                "columns": ["", *(step.label for step in brief.steps)],
                "rows": [
                    ["Yesterday", *(cell(step.yesterday) for step in brief.steps)],
                    ["Last 7 days, daily average", *(cell(step.average) for step in brief.steps)],
                ],
            }
        if brief.new:
            sections.append(
                {
                    "heading": "New users",
                    "items": [_new_user(v) for v in brief.new],
                }
            )
        if brief.visitors:
            sections.append(
                {
                    "heading": "Some visitors",
                    "items": [
                        {
                            "text": f"{v.name} ({v.email}): {v.line}"
                            if v.name
                            else f"{v.email}: {v.line}",
                            "url": v.replay_url,
                            "label": "Replay",
                        }
                        for v in brief.visitors
                    ],
                }
            )
        if brief.observations:
            sections.append(
                {"heading": "Some observations", "items": [{"text": o} for o in brief.observations]}
            )
    return parse(
        {"headline": visitor_journal.summary(outcome), "table": table, "sections": sections}
    )


# Output validator -> reader of the published content. Add a workflow by adding its reader.
SOURCES: dict[str, Callable[[bytes | str, Any], dict[str, Any] | None]] = {
    "visitor-journal.v1": _visitor_journal,
}


def from_output(
    validator: str | None, content: bytes | str, inputs: Any = None
) -> dict[str, Any] | None:
    """The summary of a published procedure result, or None. Never raises. `inputs` are the
    run's pinned inputs."""
    reader = SOURCES.get(validator or "")
    if reader is None:
        return None
    try:
        return reader(content, inputs)
    except Exception:  # noqa: BLE001 - a summary is never grounds to fail a published run
        return None


def supported(definition: dict[str, Any] | None) -> bool:
    """Whether runs of this workflow definition produce a summary."""
    if not isinstance(definition, dict):
        return False
    for executor in ("procedure", "code"):
        output = (definition.get(executor) or {}).get("output") or {}
        if not isinstance(output, dict):
            continue
        if output.get("validator") in SOURCES or output.get("summary") is True:
            return True
    return False


def markdown(summary: dict[str, Any]) -> str:
    """The summary as plain Markdown, for readers that are not Slack."""
    lines = [summary["headline"]]
    table = summary.get("table")
    if table:
        lines += [
            "",
            "| " + " | ".join(table["columns"]) + " |",
            "|---" * len(table["columns"]) + "|",
            *("| " + " | ".join(row) + " |" for row in table["rows"]),
        ]
    for section in summary.get("sections") or []:
        lines += ["", f"**{section['heading']}**", ""]
        lines += [
            f"- {item['text']}"
            + (f" ([{item['label']}]({item['url']}))" if item.get("url") else "")
            for item in section["items"]
        ]
    return "\n".join(lines)
