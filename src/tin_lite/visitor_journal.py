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
1.2.0), meant to be read on its own in a daily report: a table of unique visitors and the
product's conversion steps (yesterday, and the daily average over the last 7 days), a few
named visitors and a few observations. Tin keeps it bounded and plain; a brief that breaks the
shape is dropped, never fatal.

From 1.4.0 the brief also lists the people new in the window (`brief.new`), and a new person may
carry `work`: what the run found on the public web about where they work (company, role, what
the company does, a LinkedIn profile and the company's site). Links that are not a LinkedIn
profile or an https site are dropped, and so is a role or profile without the `basis` that ties
it to them. A run whose `look_up_new_people` input is off keeps no work at all, whatever the
report says (`read(content, work=False)`).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

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
# What a brief may show for each period (JOURNEYS.md's PERIODS): a weekly brief is longer.
BRIEF_LIMITS = {
    "day": {"new": 10, "visitors": 5, "observations": 5, "line": 240, "groups": 0},
    "week": {"new": 6, "visitors": 0, "observations": 5, "line": 300, "groups": 6},
}
# A weekly brief's groups (JOURNEYS.md's GROUPS): new users by how far they got, where people
# got stuck, and what returning users did.
GROUPS = ("stages", "stuck", "returning")
GROUP_LABEL = 80
GROUP_NOTE = 160
# The brief's table rows and the summary's opening word, for each period.
PERIOD_ROWS = {
    "day": ("Yesterday", "Last 7 days, daily average"),
    "week": ("This week", "The 4 weeks before, weekly average"),
}
EMAIL = re.compile(r"[^@\s]{1,64}@[a-z0-9.-]{1,253}\.[a-z]{2,63}")
LINKEDIN = re.compile(r"https://(?:www\.)?linkedin\.com/in/[^/?#\s()<>]{1,100}/?")
SITE = re.compile(r"https://[a-z0-9.-]{1,253}\.[a-z]{2,63}(?:/[^\s()<>]{0,200})?")
REPLAY = re.compile(r"https://(?:us|eu)\.posthog\.com/project/\d{1,12}/replay/[0-9A-Za-z-]{8,80}")
WINDOW = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
_STATUS = re.compile(r"^Status: (.*?)[ \t]*$", re.MULTILINE)
_EVIDENCE = re.compile(r"\A\s*```json\n(.+?)\n```\s*\Z", re.DOTALL)


@dataclass(frozen=True)
class Work:
    company: str
    role: str
    about: str
    linkedin_url: str | None
    company_url: str | None

    @property
    def headline(self) -> str:
        """Their role at the company, such as "Head of Growth at Acme"; empty if unknown."""
        return " at ".join(x for x in (self.role, self.company) if x)


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
    work: Work | None = None


@dataclass(frozen=True)
class Step:
    label: str
    # None when the product does not track the step; that is not zero.
    yesterday: int | None
    average: float | None


@dataclass(frozen=True)
class Visitor:
    email: str
    name: str
    line: str
    replay_url: str | None
    work: Work | None = None


@dataclass(frozen=True)
class Group:
    label: str
    note: str
    # Each person as the brief names them: "Fathin Dosunmu (Co-founder, Byro)".
    people: tuple[str, ...]


@dataclass(frozen=True)
class Brief:
    steps: tuple[Step, ...]
    visitors: tuple[Visitor, ...]
    observations: tuple[str, ...]
    new: tuple[Visitor, ...] = ()
    period: str = "day"
    stages: tuple[Group, ...] = ()
    stuck: tuple[Group, ...] = ()
    returning: tuple[Group, ...] = ()


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


def work_allowed(inputs) -> bool:
    """Whether the run's inputs let it keep what it found about new people's work."""
    return not isinstance(inputs, dict) or inputs.get("look_up_new_people") is not False


def read(content: bytes | str, *, work: bool = True) -> Outcome:
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
    people = _people(evidence["people"], head, replay_base, work)
    return Outcome(
        status=status,
        reason=reason.strip(),
        window_end=window["end"],
        people=people,
        counts={k: v for k, v in evidence["counts"].items() if type(v) is int}
        if isinstance(evidence["counts"], dict)
        else {},
        frictions=len(frictions),
        new_frictions=sum(1 for f in frictions if f.get("status") == "new"),
        measured_nothing=_measured_nothing(status, requests),
        brief=_brief(evidence.get("brief"), people),
    )


def validate(content: bytes | str) -> None:
    read(content)


def _text(value, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _work(value) -> Work | None:
    if not isinstance(value, dict):
        return None

    def link(key, pattern):
        url = value.get(key)
        return url if isinstance(url, str) and pattern.fullmatch(url) else None

    # A role or profile nothing ties to the person may be a namesake's.
    tied = bool(_text(value.get("basis"), 160))
    work = Work(
        company=_text(value.get("company"), 100),
        role=_text(value.get("role"), 100) if tied else "",
        about=_text(value.get("about"), 200),
        linkedin_url=link("linkedin_url", LINKEDIN) if tied else None,
        company_url=link("company_url", SITE),
    )
    return work if any(vars(work).values()) else None


def _people(rows, head: str, replay_base, work: bool = True) -> tuple[Person, ...]:
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
            work=_work(row.get("work")) if work and row.get("new") is True else None,
        )
    return tuple(found.values())


def follow_schedule(inputs: dict[str, Any], schedule: dict[str, Any] | None) -> dict[str, Any]:
    """A saved weekly schedule reads the week and a daily one the day, however the config was
    saved (Start here paces a daily pick to weekly). On demand keeps the founder's choice; a
    config pinned before 1.6.0 has no `period` and is left as it is."""
    period = {"daily": "day", "weekly": "week"}.get((schedule or {}).get("cadence"))
    return {**inputs, "period": period} if period and "period" in inputs else inputs


def _count(value, kind):
    if value is None or type(value) is bool:
        return None
    return kind(value) if isinstance(value, int | float) and value >= 0 else None


def _brief(value, people: tuple[Person, ...]) -> Brief | None:
    """The founder's brief, kept only when it is the expected shape. Visitors must be people
    the report names, so the brief never introduces anyone People would not."""
    if not isinstance(value, dict):
        return None
    period = value.get("period") if value.get("period") in BRIEF_LIMITS else "day"
    limit = BRIEF_LIMITS[period]
    funnel = value.get("funnel")
    steps: tuple[Step, ...] = ()
    if isinstance(funnel, dict) and isinstance(funnel.get("steps"), list):
        steps = tuple(
            Step(
                label=_text(x.get("label"), 40),
                yesterday=_count(x.get("yesterday"), int),
                average=_count(x.get("average"), float),
            )
            for x in funnel["steps"][:3]
            if isinstance(x, dict) and _text(x.get("label"), 40)
        )
    named = {p.email: p for p in people}

    def listed(key, cap):
        rows = value.get(key) if isinstance(value.get(key), list) else []
        return tuple(
            Visitor(
                email=email,
                name=named[email].name,
                line=_text(row.get("line"), limit["line"]),
                replay_url=named[email].replay_url,
                work=named[email].work,
            )
            for row in rows[:cap]
            if isinstance(row, dict)
            and (email := _text(row.get("email"), 320).lower()) in named
            and _text(row.get("line"), limit["line"])
        )

    new = listed("new", limit["new"])
    visitors = listed("visitors", limit["visitors"])
    notes = value.get("observations") if isinstance(value.get("observations"), list) else []
    observations = tuple(
        text
        for row in notes[: limit["observations"]]
        if isinstance(row, dict) and (text := _text(row.get("text"), limit["line"]))
    )

    def grouped(key):
        rows = value.get(key) if isinstance(value.get(key), list) else []
        found = []
        for row in rows[: limit["groups"]]:
            people = row.get("people") if isinstance(row, dict) else None
            names = tuple(
                _named(named[email])
                for x in (people if isinstance(people, list) else [])
                if isinstance(x, dict) and (email := _text(x.get("email"), 320).lower()) in named
            )
            if names and (label := _text(row.get("label"), GROUP_LABEL)):
                found.append(Group(label, _text(row.get("note"), GROUP_NOTE), names))
        return tuple(found)

    groups = {key: grouped(key) for key in GROUPS}
    if not (steps or new or visitors or observations or any(groups.values())):
        return None
    return Brief(
        steps=steps,
        visitors=visitors,
        observations=observations,
        new=new,
        period=period,
        **groups,
    )


def _named(person: Person) -> str:
    """A person as a group names them, with their work when it is known."""
    who = person.name or person.email
    work = person.work
    short = ", ".join(x for x in (work.role, work.company) if x) if work else ""
    return f"{who} ({short})" if short else who


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
    if outcome.brief is not None and outcome.brief.steps:
        line = (
            PERIOD_ROWS[outcome.brief.period][0]
            + ": "
            + ", ".join(
                f"{step.label.lower()} not tracked"
                if step.yesterday is None
                else f"{step.yesterday} {step.label[:1].lower()}{step.label[1:]}"
                for step in outcome.brief.steps
            )
            + "."
        )
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
