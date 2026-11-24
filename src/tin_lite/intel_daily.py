"""A client's daily market intelligence (`intel.daily`), docs/daily-intelligence.md.

Every morning at 09:00 in the project's timezone, code reads the client's market for the last
day: its trade press, vendors, regulators and journals (`intel/sources/rss.json`), its Hacker
News queries and subreddits (`intel/sources/hn-reddit.json`), its private X List (the saved
workflow's `x_list_id`) and today's world stories from Tin's world feed. The client judge
(`intel.client_judge`, Antigravity on Gemini 4 Neon) then decides what matters for the client
against its `company.md`, records the day's stories, writes a detailed report and a short
executive brief. Tin publishes the report, the brief and the fetch status under
`intel/reports/<day>/`, and posts the brief in Slack when the saved workflow asks for it.

Temporal history holds the run ID, the fetch group names, counts and the judge's identifiers;
configs, items, stories, the brief and every message stay in Postgres and project Files.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from tin_lite.intel_fetch import config as source_config
from tin_lite.intel_fetch import hn_reddit, register_group, rss
from tin_lite.workflow_diagrams import DiagramEdge, DiagramNode, WorkflowDiagram

KEY = "intel.daily"
WORKFLOW_ID = UUID("c41c0ac2-f115-4d05-9c12-ae106354e8fd")
JUDGE_WORKFLOW_ID = UUID("1d1b094c-29c7-4f5c-8d4f-007328c24511")
JUDGE_KEY = "intel.client_judge"
SCOPE = "client"
POLICY = {"version": "intel-v1"}
VERSION_LABEL = "1.0.0"
DEFAULT_SCHEDULE = {"cadence": "daily", "local_time": "09:00"}

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "project_id": {"type": "string", "format": "uuid"},
        "post_brief": {"type": "boolean", "default": True},
        "x_list_id": {"type": ["string", "null"], "pattern": "^[0-9]{1,19}$"},
        "x_keep_retweets": {"type": "boolean", "default": False},
        "lookback_hours": {"type": "integer", "minimum": 12, "maximum": 72, "default": 24},
    },
    "required": ["project_id"],
}

# The client's own files: who it is (the judge's claim rules live in its section 7) and what
# to read. company.md sits at the project root; older projects keep it under context/.
COMPANY_PATHS = ("company.md", "context/company.md")
RSS_PATH = "intel/sources/rss.json"
HN_REDDIT_PATH = "intel/sources/hn-reddit.json"
CONFIG_PATHS = (RSS_PATH, HN_REDDIT_PATH)
CONFIG_MAX_BYTES = 512_000

REPORT_DIR = "intel/reports/{day}"
REPORT_NAMES = ("report.md", "brief.md", "sources.json")
LIMITS = {"report.md": 220_000, "brief.md": 20_000, "sources.json": 400_000}

# Lookback when there is no recent earlier fetch to continue from; an earlier one within
# CONTINUE_WITHIN is continued with an hour of overlap.
CONTINUE_WITHIN_HOURS = 72
OVERLAP_HOURS = 1

# Fetch groups: each is one activity that can retry on its own. X is not a config source:
# it reads the List the saved workflow names, under per-page receipts.
GROUP_METHODS = {
    "rss": ("rss",),
    "gnews": ("google_news",),
    "pages": ("html_links", "sitemap", "crossref"),
    "hn": ("hn",),
    "reddit": ("reddit",),
}
X_GROUP = "x"
GROUPS = (*GROUP_METHODS, X_GROUP)
GROUP_USER_AGENT = {
    "rss": rss.USER_AGENT,
    "gnews": rss.USER_AGENT,
    "pages": rss.USER_AGENT,
    "hn": hn_reddit.USER_AGENT,
    "reddit": hn_reddit.USER_AGENT,
}
METHODS = {**rss.METHODS, **hn_reddit.METHODS}


def check(source: dict) -> str | None:
    if source["method"] in rss.METHODS:
        return rss.check(source)
    return hn_reddit.check(source)


source_config.register_checks(SCOPE, set(METHODS), check)


def _selector(methods: tuple[str, ...]):
    def select(config: dict) -> list[dict]:
        return [s for s in config.get("sources") or [] if s.get("method") in methods]

    return select


for _name, _methods in GROUP_METHODS.items():
    register_group(
        SCOPE, _name, _selector(_methods), {m: METHODS[m] for m in _methods if m in METHODS}
    )


class ConfigMissing(LookupError):
    """Neither of the client's source configs exists."""


def combine(raw: dict[str, bytes | None]) -> tuple[dict, list[dict]]:
    """The client's one config from its two files: rss.json's sources and settings, and
    hn-reddit.json expanded into sources. Returns the config and a status for each file that
    couldn't be read (the other one still runs)."""
    if not any(raw.get(path) for path in CONFIG_PATHS):
        raise ConfigMissing("no source config")
    sources: list[dict] = []
    settings: dict[str, Any] = {}
    problems: list[dict] = []
    body = raw.get(RSS_PATH)
    if body:
        try:
            loaded = source_config.load(body)
        except source_config.ConfigError as exc:
            problems.append(_file_problem(RSS_PATH, str(exc)))
        else:
            sources += [s for s in loaded["sources"] if isinstance(s, dict)]
            settings["rss"] = rss.settings(loaded)
    body = raw.get(HN_REDDIT_PATH)
    if body:
        try:
            loaded = source_config.load(body, require_sources=False)
        except source_config.ConfigError as exc:
            problems.append(_file_problem(HN_REDDIT_PATH, str(exc)))
        else:
            expanded, found = hn_reddit.expand(loaded)
            sources += expanded
            settings.update(found)
    if len(sources) > source_config.MAX_SOURCES:
        problems.append(
            _file_problem(
                "intel/sources",
                f"the configs list more than {source_config.MAX_SOURCES} sources; Tin read the "
                "first ones",
            )
        )
        sources = sources[: source_config.MAX_SOURCES]
    return {"sources": sources, "settings": settings}, problems


def _file_problem(path: str, reason: str) -> dict:
    return {
        "id": "config:" + re.sub(r"[^A-Za-z0-9_.-]+", "-", path)[:90],
        "name": path,
        "method": "config",
        "status": "failed",
        "error": f"Skipped: {reason}."[:300],
        "items_in_window": 0,
    }


def group_settings(group: str, config: dict) -> dict:
    found = config.get("settings") or {}
    if group in {"rss", "gnews", "pages"}:
        return dict(found.get("rss") or rss.settings({}))
    if group in {"hn", "reddit"}:
        return {
            **{name: found[name] for name in ("hn", "reddit") if name in found},
            "method_timeouts": dict(hn_reddit.METHOD_TIMEOUTS),
        }
    return {}


def report_paths(day: str) -> dict[str, str]:
    folder = REPORT_DIR.format(day=day)
    return {name: f"{folder}/{name}" for name in REPORT_NAMES}


TITLE = "Read your market every morning"
DESCRIPTION = (
    "Every morning, read what the client's market said and did in the last day: trade press, "
    "vendors, regulators and journals from intel/sources/rss.json, Hacker News and Reddit "
    "threads from intel/sources/hn-reddit.json, the client's private X List, and today's "
    "world stories from Tin's world feed. An analyst agent judges it against company.md and "
    "its claim rules, keeps the day's stories, and writes a detailed report other workflows "
    "draft from and a short executive brief. Tin publishes both with the day's source status "
    "in intel/reports/<day>/ and can post the brief in Slack. Needs company.md and at least "
    "one source config; X needs the member who connected X to save it."
)
JUDGE_TITLE = "Judge the client's day"
JUDGE_DESCRIPTION = (
    "The analyst inside the daily market brief: reads the day's items through the intel "
    "command, decides what matters for the client against company.md and its claim rules, "
    "records the day's stories, and writes the report and the executive brief. Started by "
    "the daily market brief, never on its own."
)

PRESENTATION = WorkflowDiagram(
    direction="TD",
    nodes=(
        DiagramNode(
            "configs",
            "store",
            "Every morning at 09:00",
            "company.md and intel/sources, pinned",
        ),
        DiagramNode(
            "fetch",
            "step",
            "Read the market",
            "trade press, Google News, pages, HN, Reddit",
        ),
        DiagramNode("x", "surface", "The client's X List", "page by page, each page receipted"),
        DiagramNode("world", "store", "Today's world stories", "from Tin's world feed"),
        DiagramNode("day", "store", "The day's items", "one snapshot for the analyst"),
        DiagramNode(
            "judge",
            "step",
            "The analyst judges the day",
            "stories, report and brief, by company.md",
        ),
        DiagramNode(
            "files",
            "receipt",
            "Report and brief in Files",
            "intel/reports, with the day's source status",
        ),
        DiagramNode("slack", "surface", "The brief in Slack", "when the saved workflow asks"),
    ),
    edges=(
        DiagramEdge("configs", "fetch"),
        DiagramEdge("configs", "x", label="when a List is saved"),
        DiagramEdge("fetch", "day"),
        DiagramEdge("x", "day"),
        DiagramEdge("world", "day", label="missing: the brief says so"),
        DiagramEdge("day", "judge"),
        DiagramEdge("judge", "files", label="links unwrapped"),
        DiagramEdge("files", "slack"),
    ),
)

JUDGE_PRESENTATION = WorkflowDiagram(
    direction="TD",
    nodes=(
        DiagramNode("day", "store", "The day's items", "read with the intel command"),
        DiagramNode("company", "store", "company.md", "who the client is, what may be claimed"),
        DiagramNode("judge", "step", "Decide what matters", "search, read, check the evidence"),
        DiagramNode("stories", "store", "The day's stories", "kept for tomorrow's run"),
        DiagramNode("brief", "store", "The executive brief", "checked by Tin, links from the day"),
        DiagramNode("report", "receipt", "The detailed report", "eight sections, in Files"),
    ),
    edges=(
        DiagramEdge("day", "judge"),
        DiagramEdge("company", "judge"),
        DiagramEdge("judge", "stories"),
        DiagramEdge("judge", "brief"),
        DiagramEdge("judge", "report"),
    ),
)
