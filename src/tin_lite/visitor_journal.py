"""The visitor-journal.v1 output check: what a visitor journeys report says, and who it names.

`product.visitor_journeys` ends its report with one JSON evidence block (the package's
REPORT.md and JOURNEYS.md). Tin reads the Status line and that evidence before publishing. A
report whose every PostHog query was refused measured nothing: its run fails and the report
stays a saved diagnostic. Any other report is published, and the people it names, each with an
email they gave the product, are written to People afterwards (visitor_people.py).

The envelope is strict: one Status line, one evidence block, matching status and provider. A
person row that breaks the contract is left out of People rather than failing a run that
already paid for its work.

A report may open with the founder's brief (`brief` in the evidence, from visitor journeys
1.2.0): a headline and a few short lines meant to be read on their own, in a daily report.
Tin keeps it bounded and plain; a brief that breaks the shape is dropped, never fatal.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

VALIDATOR = "visitor-journal.v1"
MEASURED_NOTHING = "VisitorJournalMeasuredNothing"
MARKER = "<!-- tin-visitor-journal-v1 -->"
STATUSES = frozenset({"complete", "incomplete", "nothing to report", "invalid configuration"})
NAMED_TIERS = frozenset({"lead", "new_user", "user"})
TIER_WORDS = {"lead": "lead", "new_user": "new user", "user": "user"}
OUTCOMES = frozenset({"ok", "refused", "invalid"})
FIELDS = frozenset(
    {
        "version",
        "status",
        "status_reason",
        "provider",
        "window",
        "requests",
        "counts",
        "people",
        "frictions",
        "ledger",
        "map",
        "limitations",
    }
)
MAX_REASON = 180
MAX_PEOPLE = 60
SUMMARY_LIMIT = 300
ACTION_LIMIT = 300
BRIEF_LINE = 240
BRIEF_LINES = ("people", "money", "worth_a_look", "traffic", "cant_see")
EMAIL = re.compile(r"[^@\s]{1,64}@[a-z0-9.-]{1,253}\.[a-z]{2,63}")
REPLAY = re.compile(r"https://(?:us|eu)\.posthog\.com/project/\d{1,12}/replay/[0-9A-Za-z-]{8,80}")
WINDOW = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
_STATUS = re.compile(r"^Status: (.*?)[ \t]*$", re.MULTILINE)
_EVIDENCE = re.compile(r"\A\s*```json\n(.+?)\n```\s*\Z", re.DOTALL)


@dataclass(frozen=True)
class Person:
    email: str
    name: str
    tier: str
    summary: str
    stopped_at: str
    action: str
    last_seen: str
    replay_url: str | None


@dataclass(frozen=True)
class Brief:
    headline: str
    people: str
    do_today: tuple[str, ...]
    money: str = ""
    broken: str = ""
    worth_a_look: str = ""
    worked: str = ""
    traffic: str = ""
    cant_see: str = ""


@dataclass(frozen=True)
class Outcome:
    status: str
    reason: str
    window_end: str
    people: tuple[Person, ...] = ()
    counts: dict = field(default_factory=dict)
    frictions: int = 0
    new_frictions: int = 0
    brief: Brief | None = None
    # A plain reason when the report measured nothing; such a report is never published.
    measured_nothing: str | None = None


def read(content: bytes | str) -> Outcome:
    """Parse one report. Raises ValueError when its envelope breaks the contract."""
    text = content.decode("utf-8") if isinstance(content, bytes) else content
    text = text.replace("\r\n", "\n")
    if text.count(MARKER) != 1:
        raise ValueError("visitor journal must end with one evidence block")
    head, tail = text.split(MARKER)
    statuses = _STATUS.findall(head)
    if len(statuses) != 1 or statuses[0] not in STATUSES:
        raise ValueError("visitor journal needs one Status line with a known status")
    status = statuses[0]
    match = _EVIDENCE.fullmatch(tail)
    if match is None:
        raise ValueError("visitor journal evidence must be one fenced JSON block")
    try:
        evidence = json.loads(match.group(1))
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("visitor journal evidence is not valid JSON") from exc
    if not isinstance(evidence, dict) or not FIELDS <= set(evidence):
        raise ValueError("visitor journal evidence is missing fields")
    if (
        evidence["version"] != VALIDATOR
        or evidence["status"] != status
        or evidence["provider"] != "analytics.posthog"
    ):
        raise ValueError("visitor journal evidence does not match the report header")
    reason = evidence["status_reason"]
    if (
        not isinstance(reason, str)
        or len(reason) > MAX_REASON
        or "\n" in reason
        or (status == "complete") == bool(reason.strip())
    ):
        raise ValueError("an incomplete visitor journal needs a short status_reason")
    window = evidence["window"]
    if not isinstance(window, dict) or not all(
        isinstance(window.get(k), str) and WINDOW.fullmatch(window[k]) for k in ("start", "end")
    ):
        raise ValueError("visitor journal evidence has no window")
    requests = evidence["requests"]
    if not isinstance(requests, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("step"), str)
        or item.get("outcome") not in OUTCOMES
        for item in requests
    ):
        raise ValueError("visitor journal evidence has an invalid request record")
    replay_base = (
        (evidence["map"] or {}).get("replay_url") if isinstance(evidence["map"], dict) else None
    )
    frictions = (
        [f for f in evidence["frictions"] if isinstance(f, dict)]
        if isinstance(evidence["frictions"], list)
        else []
    )
    return Outcome(
        status=status,
        reason=reason.strip(),
        window_end=window["end"],
        people=_people(evidence["people"], head, replay_base),
        counts={k: v for k, v in evidence["counts"].items() if type(v) is int}
        if isinstance(evidence["counts"], dict)
        else {},
        frictions=len(frictions),
        new_frictions=sum(1 for f in frictions if f.get("status") == "new"),
        measured_nothing=_measured_nothing(status, requests),
        brief=_brief(evidence.get("brief")),
    )


def validate(content: bytes | str) -> None:
    read(content)


def _text(value, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _people(rows, head: str, replay_base) -> tuple[Person, ...]:
    """The named people Tin may write to People. A row that breaks the contract is skipped:
    no email, an anonymous tier, no summary, or a person the report itself never names."""
    if not isinstance(rows, list):
        return ()
    found: dict[str, Person] = {}
    for row in rows[:MAX_PEOPLE]:
        if not isinstance(row, dict):
            continue
        email = row.get("email").strip().lower() if isinstance(row.get("email"), str) else ""
        summary = _text(row.get("summary"), SUMMARY_LIMIT)
        if (
            not EMAIL.fullmatch(email)
            or row.get("tier") not in NAMED_TIERS
            or not summary
            or email not in head.lower()
            or email in found
        ):
            continue
        replay = row.get("replay_url")
        if not (
            isinstance(replay, str)
            and isinstance(replay_base, str)
            and replay.startswith(replay_base)
            and REPLAY.fullmatch(replay)
        ):
            replay = None
        found[email] = Person(
            email=email,
            name=_text(row.get("name"), 200),
            tier=row["tier"],
            summary=summary,
            stopped_at=_text(row.get("stopped_at"), 200),
            action=_text(row.get("action"), ACTION_LIMIT),
            last_seen=_text(row.get("last_seen"), 40),
            replay_url=replay,
        )
    return tuple(found.values())


def _brief(value) -> Brief | None:
    if not isinstance(value, dict):
        return None
    headline = _text(value.get("headline"), 200)
    do_today = value.get("do_today")
    items = (
        tuple(
            text
            for item in do_today[:3]
            if isinstance(item, dict) and (text := _text(item.get("text"), BRIEF_LINE))
        )
        if isinstance(do_today, list)
        else ()
    )
    if not headline or not items:
        return None

    def ref(key):
        item = value.get(key)
        return _text(item.get("text"), BRIEF_LINE) if isinstance(item, dict) else ""

    return Brief(
        headline=headline,
        do_today=items,
        broken=ref("broken"),
        worked=ref("worked"),
        **{key: _text(value.get(key), BRIEF_LINE) for key in BRIEF_LINES},
    )


def _measured_nothing(status: str, requests: list[dict]) -> str | None:
    queries = [r for r in requests if r.get("operation") == "query.hogql"]
    if status == "invalid configuration" and not queries:
        return None
    if not any(r["outcome"] == "ok" for r in queries):
        return "every PostHog query was refused or failed; the saved diagnostic lists each one"
    return None


def summary(outcome: Outcome) -> str:
    """The run's one-line summary."""
    if outcome.status == "invalid configuration":
        return f"Visitor journeys could not start: {outcome.reason.rstrip('.')}."
    if outcome.status == "nothing to report":
        return "Nobody outside the team came in the window."
    if outcome.brief is not None:
        line = outcome.brief.headline.rstrip(".") + "."
        if outcome.status == "incomplete":
            line += f" Incomplete: {outcome.reason.rstrip('.')}."
        return line
    tiers = {tier: sum(1 for p in outcome.people if p.tier == tier) for tier in TIER_WORDS}
    parts = [
        f"{n} {TIER_WORDS[tier]}{'s' if n != 1 else ''}"
        for tier, n in (
            ("new_user", tiers["new_user"]),
            ("lead", tiers["lead"]),
            ("user", tiers["user"]),
        )
        if n
    ]
    people = outcome.counts.get("people")
    line = (", ".join(parts) or "No one named") + (
        f" among {people} visitors" if isinstance(people, int) else ""
    )
    if outcome.frictions:
        line += f"; {outcome.frictions} friction{'s' if outcome.frictions != 1 else ''}"
        if outcome.new_frictions:
            line += f" ({outcome.new_frictions} new)"
    line += "."
    if outcome.status == "incomplete":
        line = f"Incomplete ({outcome.reason.rstrip('.')}). {line}"
    return line


def failure(outcome: Outcome) -> str:
    return (
        f"Visitor journeys measured nothing: {outcome.measured_nothing}. "
        "Its diagnostic is saved with this run and was not added to Files."
    )
