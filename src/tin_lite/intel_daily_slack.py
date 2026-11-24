"""The client's executive brief, as Slack Block Kit and as Markdown for Files.

The judge writes the brief as checked JSON (`intel-brief/1`, intel_brief.validate); this module
only renders it. What code owns, the brief can't claim: each development's outlets and their
links come from its story's items, and every link in the text was checked against the day's
items before it got here. Text is escaped and nothing in it can turn into a link Slack makes by
itself (intel_slack.plain). The brief's "worth doing" suggestions stay in the report: the
Slack message and brief.md carry the developments only. Code spans are flattened, so a page
reads as words, never as a path. The layout follows Tin's RB2B and People messages: a header,
the day in bold, a context line of counts, one section per development with its outlets in
small text below, a button to the full report, and a footer naming the client.
"""

from __future__ import annotations

import re
from datetime import date

from tin_lite.intel_brief import LINK
from tin_lite.intel_slack import (
    MAX_BLOCKS,
    TEXT_LIMIT,
    button,
    context,
    cut,
    day_short,
    day_title,
    header,
    link,
    mrkdwn,
    plain,
    section,
)

KIND_MARKERS = {
    "competitor": "⚔️",
    "platform": "🧩",
    "buyer": "🗣️",
    "regulation": "⚖️",
    "market": "📈",
    "world": "🌍",
    "other": "📌",
}
VERIFY = "🔎 To verify"
OUTLETS = 2
SOURCE_KINDS = (
    ("x", "X List"),
    ("rss", "trade press"),
    ("hn", "HN"),
    ("reddit", "Reddit"),
)


def _flat(text: str) -> str:
    """No code spans or Slack emphasis markers from the model: words, not paths."""
    return re.sub(r"[`*_~]", "", text or "")


def _join(words: list[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def source_kinds(counts: dict[str, int]) -> str:
    """Which kinds of source carried items today, in words."""
    return _join([label for kind, label in SOURCE_KINDS if counts.get(kind)])


def outlets(story: dict | None, links: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """Up to two of a story's outlets, strongest first (the order the judge attached them),
    each with the link to its item."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for entry in (story or {}).get("items") or []:
        url = (links or {}).get(entry.get("url"), entry.get("url"))
        name = (entry.get("source") or "").strip()
        if not name or name in seen or not isinstance(url, str) or not url.startswith("https://"):
            continue
        if name == "World stories":
            name = "Tin's world feed"
        seen.add(name)
        found.append((name, url))
        if len(found) == OUTLETS:
            break
    return found


def notes(fetch: dict, *, brief_source: str | None, world_status: str | None) -> list[str]:
    """Plain lines on what the day lacked: world stories, X, a brief Tin wrote itself."""
    lines = []
    if world_status in (None, "missing"):
        lines.append("World stories unavailable today")
    for status in fetch.get("source_status") or []:
        if status.get("id") == "x-list" and status.get("reason"):
            reason = status["reason"]
            lines.append(
                {
                    "x_member": "X List not read: only the member who connected X can read it",
                    "x_reauthorize": "X List not read: reconnect X in Integrations",
                    "x_not_connected": "X List not read: X isn't connected",
                    "x_rate_limited": "X List cut short: X asked Tin to wait",
                    "x_uncertain": "X List cut short: one page wasn't confirmed",
                    "x_invalid": "X List not read: X refused the List ID",
                }.get(reason, "X List not fully read")
            )
    if brief_source == "fallback":
        lines.append("Tin wrote this brief from the day's top stories")
    return lines


def client_message(
    brief: dict,
    stories: dict[str, dict],
    *,
    client_name: str,
    day: date,
    items: int,
    sources: int,
    kinds: str,
    report_url: str | None,
    sources_url: str | None,
    links: dict[str, str] | None = None,
    extra_notes: list[str] | None = None,
) -> dict:
    """The brief (intel-brief/1) as Block Kit."""
    headline = brief.get("headline") or ""
    developments = brief.get("items") or []
    counts = f"{items:,} items from {sources:,} sources · {len(developments)} developments"
    if kinds:
        counts += f" · {kinds}"
    blocks: list[dict] = [
        header(f"{client_name} · {day_title(day)}"),
        section(f"*{plain(_flat(headline))}*"),
        context(plain(counts)),
        {"type": "divider"},
    ]
    for entry in developments:
        marker = KIND_MARKERS.get(entry.get("kind") or "other", KIND_MARKERS["other"])
        lead = plain(_flat(entry.get("lead") or ""))
        body = mrkdwn(_flat_links(entry.get("body") or ""), links)
        blocks.append(section(cut(f"{marker}  *{lead}*\n{body}", 2900)))
        small = []
        if entry.get("verify"):
            note = plain(_flat(entry.get("verify_note") or "one secondary source"))
            small.append(f"{VERIFY}: {note}")
        story = stories.get(entry.get("story") or "")
        small += [link(url, name) for name, url in outlets(story, links)]
        if small:
            blocks.append(context(" · ".join(small)))
    footer_notes = [plain(line) for line in extra_notes or []]
    if brief.get("note"):
        footer_notes.append(plain(_flat(brief["note"])))
    if footer_notes:
        blocks.append(context(" · ".join(footer_notes)))
    blocks.append({"type": "divider"})
    buttons = []
    if report_url:
        buttons.append(button("Read the full report", report_url, "report", primary=True))
    if sources_url:
        buttons.append(button("Sources today", sources_url, "sources"))
    if buttons:
        blocks.append({"type": "actions", "elements": buttons})
    blocks.append(context(f"{plain(client_name)} · Tin daily intelligence"))
    text = cut(f"{client_name} market brief, {day_short(day)}: {_flat(headline)}", TEXT_LIMIT)
    return {"text": plain(text), "blocks": blocks[:MAX_BLOCKS]}


def _flat_links(text: str) -> str:
    """Flatten code and emphasis outside the [words](url) links, keeping the links intact."""
    out, last = [], 0
    for match in LINK.finditer(text or ""):
        out.append(_flat(text[last : match.start()]))
        out.append(f"[{_flat(match.group(1))}]({match.group(2)})")
        last = match.end()
    out.append(_flat((text or "")[last:]))
    return "".join(out)


def _md_links(text: str, links: dict[str, str] | None) -> str:
    links = links or {}
    return LINK.sub(
        lambda m: f"[{m.group(1)}]({links.get(m.group(2), m.group(2))})",
        _flat_links(text),
    )


def brief_markdown(
    brief: dict,
    stories: dict[str, dict],
    *,
    client_name: str,
    day: date,
    items: int,
    sources: int,
    links: dict[str, str] | None = None,
    extra_notes: list[str] | None = None,
) -> str:
    """The same brief as Markdown, for intel/reports/<day>/brief.md."""
    lines = [
        f"# {client_name} market brief, {day_title(day)}",
        "",
        f"**{_flat(brief.get('headline') or '')}**",
        "",
        f"{items:,} items from {sources:,} sources.",
    ]
    for entry in brief.get("items") or []:
        marker = KIND_MARKERS.get(entry.get("kind") or "other", KIND_MARKERS["other"])
        lines += ["", f"## {marker} {_flat(entry.get('lead') or '')}", ""]
        lines.append(_md_links(entry.get("body") or "", links))
        if entry.get("verify"):
            lines += ["", f"To verify: {_flat(entry.get('verify_note') or 'one secondary source')}"]
        found = outlets(stories.get(entry.get("story") or ""), links)
        if found:
            lines += ["", "Sources: " + ", ".join(f"[{name}]({url})" for name, url in found)]
    tail = list(extra_notes or [])
    if brief.get("note"):
        tail.append(_flat(brief["note"]))
    if tail:
        lines += ["", "---", ""] + [f"{line}." if not line.endswith(".") else line for line in tail]
    return "\n".join(lines).rstrip() + "\n"


def google_links(brief: dict, stories: dict[str, dict]) -> list[str]:
    """Every Google News link the rendered brief uses: in its text and its outlets' links."""
    found: list[str] = []
    texts = [brief.get("headline") or "", brief.get("note") or ""]
    for entry in brief.get("items") or []:
        texts.append(entry.get("body") or "")
        story = stories.get(entry.get("story") or "")
        found += [url for _name, url in outlets(story)]
    for text in texts:
        found += [m.group(2) for m in LINK.finditer(text)]
    return list(dict.fromkeys(u for u in found if u.startswith("https://news.google.com/")))
