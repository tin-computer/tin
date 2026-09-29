"""Awesome-list submission as a native flow: read the submission packets an awesome-lists run
wrote, place each entry line in the list's current file with code, and render the exact changes
the founder approves. After approval the activities fork each list with the founder's own GitHub
account and open one pull request (or issue, when the list asks for issues) per list.

Nothing here talks to GitHub. The activities own receipts, the GitHub account and approval.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlsplit
from uuid import UUID

from tin_lite.domain import AWESOME_SUBMIT_WORKFLOW_NAME
from tin_lite.organic_audit import digest

KEY = AWESOME_SUBMIT_WORKFLOW_NAME

PREFIX = "awesome_submit"
OUTPUT_DIR = "reports/awesome-submissions"
# The procedure that finds the lists and writes the packets; a project may trial a copy.
SOURCE_KEYS = frozenset({"outreach.awesome_lists", "custom.awesome_lists"})
SOURCE_DIR = "reports/awesome-lists"
SOURCE_MAX_BYTES = 250_000
PLAN_DOCS = {"PLAN.md": 120_000}
RESULT_DOCS = {"RESULT.md": 120_000}
MAX_SUBMISSIONS = 5
MAX_LIST_FILE_BYTES = 1_000_000
BLOCK = re.compile(r"```json awesome-submissions\s*\n(.*?)\n```", re.S)
REPOSITORY = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
LIST_PATH = re.compile(r"^(?!/)(?!.*\.\.)[A-Za-z0-9._/-]{1,200}\.(?:md|markdown)$", re.I)
HEADING = re.compile(r"^(#{1,6})\s+\S")
ENTRY_MARKERS = ("- ", "* ", "+ ")
LINK_TEXT = re.compile(r"\[([^\]]{1,200})\]\(")
METHODS = ("pull_request", "issue")
ORDERS = ("alphabetical", "end")
DISCLOSURE = (
    "I maintain {product}. I prepared this entry with Tin and reviewed it myself before sending."
)

INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["project_id"],
    "properties": {
        "project_id": {"type": "string", "format": "uuid"},
        "source_run_id": {
            "type": "string",
            "format": "uuid",
            "title": "Awesome lists run (optional)",
            "description": "Leave empty to use this project's latest awesome lists report.",
            "x-tin-ui": {"control": "text", "order": 10},
        },
        "lists": {
            "type": "array",
            "title": "Only these lists (optional)",
            "description": "owner/name of lists from the report; empty submits its top picks.",
            "maxItems": MAX_SUBMISSIONS,
            "items": {"type": "string", "maxLength": 141},
            "default": [],
            "x-tin-ui": {"order": 20},
        },
    },
}


class PacketError(ValueError):
    """A submission packet or list file is outside what Tin will send; the text is Tin's own."""


def source_path(run_id) -> str:
    """Where an awesome lists run writes its report: one file per run, kept as history."""
    return f"{SOURCE_DIR}/{UUID(str(run_id))}.md"


def paths(run_id: str, names) -> dict[str, str]:
    return {name: f"{OUTPUT_DIR}/{UUID(str(run_id))}/{name}" for name in names}


def check_inputs(inputs: dict) -> None:
    if inputs.get("source_run_id"):
        UUID(str(inputs["source_run_id"]))
    lists = inputs.get("lists") or []
    if not isinstance(lists, list) or len(lists) > MAX_SUBMISSIONS:
        raise ValueError(f"Choose at most {MAX_SUBMISSIONS} lists.")
    for item in lists:
        if not isinstance(item, str) or not REPOSITORY.fullmatch(item.strip()):
            raise ValueError("Name each list as owner/name.")


def _text(value, field: str, *, lo: int, hi: int, line: bool = True) -> str:
    if not isinstance(value, str):
        raise PacketError(f"The packet's {field} is missing.")
    value = value.strip()
    if not lo <= len(value) <= hi:
        raise PacketError(f"The packet's {field} must be {lo} to {hi} characters.")
    if line and ("\n" in value or "\r" in value):
        raise PacketError(f"The packet's {field} must be one line.")
    if any(ord(c) < 32 and c not in "\n\r\t" for c in value):
        raise PacketError(f"The packet's {field} contains control characters.")
    return value


def _url(value, field: str) -> str:
    value = _text(value, field, lo=8, hi=500)
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname:
        raise PacketError(f"The packet's {field} must be an https URL.")
    return value


def url_key(url: str) -> str:
    """Host and path without scheme, www, query or trailing slash: how a list would link it."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    return f"{host}{parts.path.rstrip('/')}".lower()


def parse_packets(report: str) -> dict:
    """The one fenced submissions block at the end of an awesome-lists report, validated."""
    blocks = BLOCK.findall(report)
    if len(blocks) != 1:
        raise PacketError("The awesome lists report has no submissions block to send.")
    try:
        raw = json.loads(blocks[0])
    except (ValueError, RecursionError):
        raise PacketError("The report's submissions block is not valid JSON.") from None
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise PacketError("The report's submissions block has an unsupported version.")
    product = raw.get("product")
    if not isinstance(product, dict):
        raise PacketError("The submissions block does not name the product.")
    name = _text(product.get("name"), "product name", lo=1, hi=80)
    links = [_url(product.get("url"), "product URL")]
    if product.get("repository_url"):
        links.append(_url(product["repository_url"], "repository URL"))
    items = raw.get("submissions")
    if not isinstance(items, list) or len(items) > 20:
        raise PacketError("The submissions block has no usable list of submissions.")
    submissions, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise PacketError("A submission is not an object.")
        submission = packet(item, links)
        if submission["list"].lower() in seen:
            raise PacketError("The submissions block names a list twice.")
        seen.add(submission["list"].lower())
        submissions.append(submission)
    return {"product": {"name": name, "links": links}, "submissions": submissions}


def packet(item: dict, links: list[str]) -> dict:
    repository = _text(item.get("list"), "list", lo=3, hi=141)
    if not REPOSITORY.fullmatch(repository):
        raise PacketError("A submission's list must be owner/name.")
    method = item.get("method", "pull_request")
    if method not in METHODS:
        raise PacketError("A submission's method must be pull_request or issue.")
    title = _text(item.get("title"), "title", lo=5, hi=120)
    body = _text(item.get("body"), "body", lo=20, hi=5000, line=False)
    entry = _text(item.get("entry"), "entry", lo=10, hi=400)
    if not any(url_key(link) in entry.lower() for link in links):
        raise PacketError(f"The entry for {repository} does not link the product.")
    result = {"list": repository, "method": method, "title": title, "body": body, "entry": entry}
    if method == "pull_request":
        path = _text(item.get("path", "README.md"), "path", lo=4, hi=200)
        if not LIST_PATH.fullmatch(path):
            raise PacketError(f"The list file for {repository} must be a Markdown path.")
        section = _text(item.get("section"), "section", lo=2, hi=200)
        if not HEADING.match(section):
            raise PacketError(f"The section for {repository} must be a Markdown heading line.")
        if not entry.startswith(ENTRY_MARKERS):
            raise PacketError(f"The entry for {repository} must be a list item.")
        order = item.get("order", "alphabetical")
        if order not in ORDERS:
            raise PacketError("A submission's order must be alphabetical or end.")
        result |= {
            "path": path,
            "section": section,
            "order": order,
            "commit_message": _text(
                item.get("commit_message") or title, "commit message", lo=5, hi=120
            ),
        }
    return result


def select(packets: dict, wanted: list[str]) -> list[dict]:
    """The founder's named lists, in their order, or the report's top picks."""
    submissions = packets["submissions"]
    if not wanted:
        return submissions[:MAX_SUBMISSIONS]
    by_name = {s["list"].lower(): s for s in submissions}
    chosen = []
    for name in wanted:
        found = by_name.get(name.strip().lower())
        if found is None:
            raise PacketError(f"The awesome lists report has no submission for {name}.")
        chosen.append(found)
    return chosen


def with_disclosure(body: str, product: str) -> str:
    line = DISCLOSURE.format(product=product)
    return body if line in body else f"{body.rstrip()}\n\n{line}"


def _sort_key(line: str) -> str:
    match = LINK_TEXT.search(line)
    text = match.group(1) if match else line[2:]
    return re.sub(r"[^0-9a-z]+", "", text.casefold())


def place(content: str, *, section: str, entry: str, order: str, links: list[str]) -> dict:
    """Insert one entry line in a section of the list's current file.

    Refuses when the product is already listed, the heading is missing or ambiguous, or the
    section has no top-level items to line up with; code never guesses where a line goes.
    """
    lower = content.lower()
    for link in links:
        if url_key(link) and url_key(link) in lower:
            raise PacketError("The product is already in this list.")
    newline = "\r\n" if "\r\n" in content else "\n"
    lines = content.split(newline)
    wanted = section.strip()
    starts = [i for i, line in enumerate(lines) if line.rstrip() == wanted]
    if len(starts) != 1:
        raise PacketError(
            "The list no longer has that section." if not starts else "The section is ambiguous."
        )
    start = starts[0]
    level = len(HEADING.match(wanted).group(1))
    end = len(lines)
    for i in range(start + 1, len(lines)):
        heading = HEADING.match(lines[i])
        if heading and len(heading.group(1)) <= level:
            end = i
            break
    marker = entry[:2]
    items = [i for i in range(start + 1, end) if lines[i].startswith(marker)]
    if not items:
        raise PacketError("The section has no list items to line the entry up with.")
    last = items[-1]
    # A top-level item can carry indented sub-items; insert after them, never between.
    while last + 1 < end and lines[last + 1].startswith((" ", "\t")) and lines[last + 1].strip():
        last += 1
    at = last + 1
    if order == "alphabetical":
        key = _sort_key(entry)
        for i in items:
            if _sort_key(lines[i]) > key:
                at = i
                break
    updated = [*lines[:at], entry, *lines[at:]]
    return {
        "content": newline.join(updated),
        "line": at + 1,
        "context": {
            "before": lines[max(start, at - 2) : at],
            "after": lines[at : min(end, at + 2)],
        },
    }


def plan_digest(changes: list[dict]) -> str:
    return digest(changes)


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


def render_plan(*, account: str, product: str, changes: list[dict], skipped: list[dict]) -> str:
    """What the founder approves: every exact change, and the account it is sent from."""
    out = [
        f"# Awesome list submissions for {product}",
        "",
        f"Approving sends {len(changes)} submission{'s' if len(changes) != 1 else ''} from "
        f"your GitHub account **@{account}**. Nothing is sent before you approve, and Tin never "
        "submits to the same list twice.",
        "",
    ]
    for n, change in enumerate(changes, 1):
        out += [f"## {n}. {change['list']}", ""]
        if change["method"] == "pull_request":
            out += [
                f"Pull request against `{change['path']}` at "
                f"`{change['base_commit'][:12]}`, in **{change['section'].lstrip('# ')}**, "
                f"line {change['line']}. Tin forks the list to @{account} first.",
                "",
                "```diff",
                *[f" {line}" for line in change["context"]["before"]],
                f"+{change['entry']}",
                *[f" {line}" for line in change["context"]["after"]],
                "```",
                "",
                f"**Title:** {change['title']}",
                f"**Commit:** {change['commit_message']}",
            ]
        else:
            out += [
                "Issue, because this list takes submissions as issues.",
                "",
                f"**Title:** {change['title']}",
            ]
        out += ["", "**Description:**", "", _quote(change["body"]), ""]
    if skipped:
        out += ["## Not included", ""]
        out += [f"- **{s['list']}**: {s['reason']}" for s in skipped]
        out += [""]
    return "\n".join(out).rstrip() + "\n"


def render_result(*, account: str, product: str, results: list[dict], skipped: list[dict]) -> str:
    out = [f"# Awesome list submissions for {product}", ""]
    sent = [r for r in results if r.get("status") == "completed"]
    out += [
        f"Sent {len(sent)} of {len(results)} approved submissions from @{account}. "
        "Maintainers review them on their own schedule; reply to their comments from GitHub.",
        "",
        "| List | Sent as | Result |",
        "|---|---|---|",
    ]
    for r in results:
        if r.get("status") == "completed":
            kind = "pull request" if r["method"] == "pull_request" else "issue"
            outcome = f"[{kind} #{r['number']}]({r['url']})"
        else:
            outcome = r.get("reason") or "not sent"
        out.append(f"| {r['list']} | {r['method'].replace('_', ' ')} | {outcome} |")
    if skipped:
        out += ["", "## Not included", ""]
        out += [f"- **{s['list']}**: {s['reason']}" for s in skipped]
    return "\n".join(out).rstrip() + "\n"


def summary_line(results: list[dict]) -> str:
    sent = sum(1 for r in results if r.get("status") == "completed")
    if not results:
        return "No awesome list submission was sent."
    return f"Sent {sent} of {len(results)} awesome list submissions from your GitHub account."
