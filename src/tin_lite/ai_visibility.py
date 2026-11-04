"""AI Visibility: how often AI answer engines name and recommend a project, week by week.

A week's report holds every answer a panel of answer engines gave to the project's panel
questions, each judged as recommending the project, mentioning (or citing) it, or neither, with
the answer's quote, the engine's search queries and the sources it cited. Reports are produced
outside Tin for now and an operator imports them (`tin-lite import-ai-visibility`); Tin stores
one per project and week in Postgres and the AI Visibility page reads only that.

The page shows one week as a table of questions by engines, the week's score and, once there are
several weeks, how mentions and recommendations moved. Engines are of two kinds: `web`, what
people see today (AI apps, and models with web search on), and `memory`, models with web search
off. Each kind is scored on its own; questions marked `in_score: false` (a branded comparison,
say) are shown but left out of every score.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

# Runaway guards, well above a real panel (25 questions, 15 engines, 2 answers each).
MAX_QUESTIONS = 200
MAX_ENGINES = 40
MAX_ANSWERS = 20_000
MAX_REPORT_BYTES = 20_000_000
OUTCOMES = ("recommended", "mentioned", "none", "no_answer")
RANK = {"recommended": 3, "mentioned": 2, "none": 1, "no_answer": 0}
KINDS = ("web", "memory")
# Engine marks shipped in static/ai-engines/; any other engine shows its initials.
ICONS = ("openai", "claude", "gemini", "google", "perplexity", "kimi", "zai")
MAX_ANSWER_CHARS = 60_000
TOP_RIVALS = 3


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


Key = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")]


class Engine(_Strict):
    key: Key
    name: str = Field(min_length=1, max_length=60)
    detail: str = Field(default="", max_length=60)
    kind: Literal["web", "memory"]
    icon: Literal["openai", "claude", "gemini", "google", "perplexity", "kimi", "zai"] | None = None


class Section(_Strict):
    key: Key
    title: str = Field(min_length=1, max_length=120)


class Group(_Strict):
    key: Key
    section: Key
    title: str = Field(min_length=1, max_length=160)
    note: str = Field(default="", max_length=200)


class Question(_Strict):
    id: Key
    group: Key
    text: str = Field(min_length=1, max_length=2000)
    in_score: bool = True


class Source(_Strict):
    url: str = Field(min_length=1, max_length=2000)
    domain: str = Field(default="", max_length=255)
    title: str = Field(default="", max_length=300)


class Answer(_Strict):
    question: Key
    engine: Key
    take: int = Field(ge=1, le=10)
    outcome: Literal["recommended", "mentioned", "none", "no_answer"]
    quote: str = Field(default="", max_length=2000)
    # The whole answer as the engine wrote it (usually Markdown).
    text: str = Field(default="", max_length=MAX_ANSWER_CHARS)
    queries: list[str] = Field(default_factory=list, max_length=100)
    cited: list[Source] = Field(default_factory=list, max_length=200)
    # Pages the engine read but didn't cite (some engines cite nothing inline).
    read: list[Source] = Field(default_factory=list, max_length=200)
    recommended_instead: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("queries", "recommended_instead")
    @classmethod
    def _short(cls, values: list[str]) -> list[str]:
        return [value.strip()[:300] for value in values if value.strip()]


class Report(_Strict):
    version: Literal[1]
    week_start: date
    collected_at: str = Field(min_length=1, max_length=40)
    # The project's site, so its pages stand out among an answer's cited sources.
    domain: str = Field(default="", max_length=255)
    takes: int = Field(ge=1, le=10)
    engines: list[Engine] = Field(min_length=1, max_length=MAX_ENGINES)
    sections: list[Section] = Field(min_length=1, max_length=20)
    groups: list[Group] = Field(min_length=1, max_length=MAX_QUESTIONS)
    questions: list[Question] = Field(min_length=1, max_length=MAX_QUESTIONS)
    answers: list[Answer] = Field(max_length=MAX_ANSWERS)

    @model_validator(mode="after")
    def _consistent(self) -> Report:
        if self.week_start.isoweekday() != 1:
            raise ValueError("week_start must be a Monday")
        for name, keys in (
            ("engine", [e.key for e in self.engines]),
            ("section", [s.key for s in self.sections]),
            ("group", [g.key for g in self.groups]),
            ("question", [q.id for q in self.questions]),
        ):
            if len(keys) != len(set(keys)):
                raise ValueError(f"each {name} key must be unique")
        sections = {s.key for s in self.sections}
        groups = {g.key for g in self.groups}
        engines = {e.key for e in self.engines}
        questions = {q.id for q in self.questions}
        if bad := [g.key for g in self.groups if g.section not in sections]:
            raise ValueError(f"groups name an unknown section: {bad[:5]}")
        if bad := [q.id for q in self.questions if q.group not in groups]:
            raise ValueError(f"questions name an unknown group: {bad[:5]}")
        seen = set()
        for answer in self.answers:
            if answer.question not in questions or answer.engine not in engines:
                raise ValueError(
                    f"an answer names an unknown question or engine: "
                    f"{answer.question}/{answer.engine}"
                )
            if answer.take > self.takes:
                raise ValueError(f"answer take {answer.take} is above takes {self.takes}")
            slot = (answer.question, answer.engine, answer.take)
            if slot in seen:
                raise ValueError(f"two answers for {slot}")
            seen.add(slot)
        return self


def parse_report(raw: bytes | str) -> Report:
    """A week's report from its JSON, validated, or ValueError naming what is wrong."""
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError(f"the report is over {MAX_REPORT_BYTES:,} bytes")
    try:
        return Report.model_validate_json(raw)
    except ValueError as exc:  # pydantic's ValidationError is a ValueError
        raise ValueError(str(exc)[:2000]) from None


# ---------------------------------------------------------------------------- scoring


def summarize(report: Report) -> dict[str, Any]:
    """The week's totals per engine kind: answers, recommended, mentioned (which includes
    recommended), questions that named the project at least once and the brands most often
    recommended instead. Only scored questions and answered answers count."""
    scored = {q.id for q in report.questions if q.in_score}
    kind = {e.key: e.kind for e in report.engines}
    out: dict[str, Any] = {}
    for k in KINDS:
        answers = [
            a
            for a in report.answers
            if kind[a.engine] == k and a.question in scored and a.outcome != "no_answer"
        ]
        rivals = Counter(name for a in answers for name in dict.fromkeys(a.recommended_instead))
        out[k] = {
            "answers": len(answers),
            "recommended": sum(a.outcome == "recommended" for a in answers),
            "mentioned": sum(a.outcome in ("recommended", "mentioned") for a in answers),
            "questions": len(scored),
            "questions_named": len(
                {a.question for a in answers if a.outcome in ("recommended", "mentioned")}
            ),
            "rivals": [{"name": n, "answers": c} for n, c in rivals.most_common(5)],
        }
    return {"week_start": report.week_start.isoformat(), **out}


def page(report: Report, weeks: list[dict[str, Any]]) -> dict[str, Any]:
    """The AI Visibility page for one week: its questions by engines (the best of each cell's
    answers and how many of them reached it), each question's leading rivals, the week's score
    with the week before, and every week's totals for the trend. `weeks` is every stored week's
    `summary`, oldest first."""
    by_cell: dict[tuple[str, str], list[Answer]] = defaultdict(list)
    for answer in report.answers:
        by_cell[(answer.question, answer.engine)].append(answer)
    kind = {e.key: e.kind for e in report.engines}
    cells: dict[str, dict[str, dict[str, Any]]] = {}
    rivals: dict[str, dict[str, list[str]]] = {}
    for question in report.questions:
        row: dict[str, dict[str, Any]] = {}
        named: dict[str, Counter] = {k: Counter() for k in KINDS}
        for engine in report.engines:
            answers = by_cell.get((question.id, engine.key), [])
            if not answers:
                continue
            best = max((a.outcome for a in answers), key=RANK.__getitem__)
            row[engine.key] = {
                "outcome": best,
                "count": sum(a.outcome == best for a in answers),
                "of": len(answers),
            }
            for a in answers:
                named[kind[engine.key]].update(list(dict.fromkeys(a.recommended_instead)))
        cells[question.id] = row
        rivals[question.id] = {k: [n for n, _ in named[k].most_common(TOP_RIVALS)] for k in KINDS}
    index = next(
        (i for i, w in enumerate(weeks) if w["week_start"] == report.week_start.isoformat()), None
    )
    current = weeks[index] if index is not None else summarize(report)
    previous = weeks[index - 1] if index else None
    return {
        "week_start": report.week_start.isoformat(),
        "collected_at": report.collected_at,
        "domain": report.domain,
        "takes": report.takes,
        "engines": [e.model_dump() for e in report.engines],
        "sections": [s.model_dump() for s in report.sections],
        "groups": [g.model_dump() for g in report.groups],
        "questions": [q.model_dump() for q in report.questions],
        "cells": cells,
        "rivals": rivals,
        "score": {k: week_score(current[k]) for k in KINDS},
        "previous": {k: week_score(previous[k]) for k in KINDS} if previous else None,
        "weeks": [
            {"week_start": w["week_start"], **{k: week_score(w[k]) for k in KINDS}} for w in weeks
        ],
    }


def week_score(totals: dict[str, Any]) -> dict[str, Any]:
    answers = totals["answers"]
    return {
        **totals,
        "recommended_rate": totals["recommended"] / answers if answers else None,
        "mentioned_rate": totals["mentioned"] / answers if answers else None,
    }


def answers_for(report: Report, question: str, engine: str) -> dict[str, Any]:
    """Every answer one engine gave to one question: outcome, quote, queries, cited sources and
    who it recommended instead."""
    q = next((q for q in report.questions if q.id == question), None)
    e = next((e for e in report.engines if e.key == engine), None)
    if q is None or e is None:
        raise LookupError("No such question or engine in this week")
    answers = sorted(
        (a for a in report.answers if a.question == question and a.engine == engine),
        key=lambda a: a.take,
    )
    return {
        "question": q.model_dump(),
        "engine": e.model_dump(),
        "answers": [a.model_dump() for a in answers],
    }


def monday(day: date) -> date:
    return day - timedelta(days=day.isoweekday() - 1)


# ---------------------------------------------------------------------------- storage


class AIVisibilityStore:
    """The stored weeks, in Postgres. Reads are by project and week only."""

    def __init__(self, database: Any) -> None:
        self.db = database

    async def save(self, project_id: UUID, report: Report) -> bool:
        """Store the week's report, replacing one already stored for that week. True when it
        replaced one."""
        replaced = await self.db.pool.fetchval(
            """INSERT INTO ai_visibility_weeks (project_id, week_start, report, summary)
               VALUES ($1, $2, $3::jsonb, $4::jsonb)
               ON CONFLICT (project_id, week_start) DO UPDATE
               SET report = EXCLUDED.report, summary = EXCLUDED.summary, imported_at = now()
               RETURNING (xmax <> 0)""",
            project_id,
            report.week_start,
            report.model_dump_json(),
            json.dumps(summarize(report)),
        )
        return bool(replaced)

    async def weeks(self, project_id: UUID) -> list[dict[str, Any]]:
        rows = await self.db.pool.fetch(
            """SELECT summary FROM ai_visibility_weeks WHERE project_id = $1
               ORDER BY week_start""",
            project_id,
        )
        return [_json(row["summary"]) for row in rows]

    async def report(self, project_id: UUID, week_start: date) -> Report | None:
        raw = await self.db.pool.fetchval(
            "SELECT report FROM ai_visibility_weeks WHERE project_id = $1 AND week_start = $2",
            project_id,
            week_start,
        )
        return None if raw is None else Report.model_validate(_json(raw))

    async def page(self, project_id: UUID, week: date | None = None) -> dict[str, Any]:
        """The page for `week` (any day in it), or the latest week. With no weeks stored the
        page says so: `{"week_start": None, "weeks": []}`."""
        weeks = await self.weeks(project_id)
        if not weeks:
            return {"week_start": None, "weeks": []}
        starts = [w["week_start"] for w in weeks]
        wanted = monday(week).isoformat() if week else starts[-1]
        if wanted not in starts:
            raise LookupError("No AI Visibility report for that week")
        report = await self.report(project_id, date.fromisoformat(wanted))
        if report is None:  # deleted between the two reads
            raise LookupError("No AI Visibility report for that week")
        return page(report, weeks)

    async def answers(
        self, project_id: UUID, week: date, question: str, engine: str
    ) -> dict[str, Any]:
        report = await self.report(project_id, monday(week))
        if report is None:
            raise LookupError("No AI Visibility report for that week")
        return answers_for(report, question, engine)


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value
