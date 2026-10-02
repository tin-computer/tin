"""organic-audit-v13: the audit's buyer questions on six AI engines, through ai_answers.

The audit's own answers come from one model with web search. v13 also asks the same frozen
questions on the engines people use, through DataForSEO (`tin_lite.ai_answers`):

- consumer_app_answer: what a person sees in the ChatGPT or Gemini app, in Google AI Mode or
  in an AI Overview. ChatGPT answers as the app does; Tin does not force a web search.
- api_model_answer: what Claude and Perplexity's Sonar model say through their APIs on the
  live endpoint, with web search. claude.ai and perplexity.ai themselves are not measured.

The two are labelled apart everywhere: rows, the report and SUMMARY.json.

The measurement has a cost ceiling: the policy's `ai_engines_max_cost_usd`, never more than
what the audit's own spending limit has left. Questions that don't fit are not asked; the
report names them. Pure functions here; the activities save, measure and publish.
"""

from __future__ import annotations

from decimal import Decimal

from tin_lite import ai_answers

STAGE = "engines"
LABELS = {
    "chatgpt": "ChatGPT app",
    "gemini": "Gemini app",
    "google_ai_mode": "Google AI Mode",
    "google_ai_overview": "Google AI Overview",
    "claude": "Claude API model",
    "perplexity": "Perplexity API model",
}
MEASUREMENTS = {
    ai_answers.CONSUMER_APP_ANSWER: (
        "In the apps people use",
        "What a person sees in the app or on the results page.",
    ),
    ai_answers.API_MODEL_ANSWER: (
        "From API models",
        "What the vendor's model says through its API, with web search. claude.ai and "
        "perplexity.ai themselves are not measured.",
    ),
}
# Only Tin-owned reason codes reach the report.
REASONS = {
    "answer_completion": "An answer completion re-reports an earlier audit and asks no engine.",
    "no_question_panel": "There were no buyer questions to ask.",
    "panel_not_measurable": "The questions or names did not fit the measurement's limits.",
    "cost_ceiling": "Not one question fit within what the audit's spending limit had left.",
    "measurement_unavailable": (
        "The measurement did not finish. Answers it bought stay in Tin's receipts and are "
        "billed; none is shown here."
    ),
    "not_started": "The measurement was not started.",
}
ROW_STATUSES = {
    "no_answer": "the engine gave no answer",
    "failed": "the request failed",
    "timeout": "not ready by the deadline",
    "unknown": "the outcome could not be confirmed and was not bought again",
}
# One row per engine in SUMMARY.json, read with dict(zip(columns, row)).
ENGINE_COLUMNS = (
    "engine",
    "measurement",
    "model",
    "asked",
    "answered",
    "mentioned",
    "cited",
    "recommended_first",
    "cost_usd",
)
MAX_CITED_URLS = 10


def per_question_usd(policy: dict) -> Decimal:
    """The pinned upper bound for one question on every engine."""
    priority = policy["ai_engines_priority"]
    return sum(
        (ai_answers.ENGINES[engine].price[priority] for engine in policy["ai_engines"]),
        Decimal(0),
    )


def question_order(panel: dict) -> list[tuple[int, str]]:
    """(audit question index, text) taking one question per buyer job in turn, so a ceiling
    that cuts the list still asks every job. Repeats and over-long questions are left out."""
    jobs: dict[str, list[tuple[int, str]]] = {}
    seen: set[str] = set()
    for index, row in enumerate(panel.get("questions") or []):
        text = " ".join(str(row.get("question") or "").split())
        if not text or len(text) > ai_answers.LIMITS["prompt_chars"]:
            continue
        if text.casefold() in seen:
            continue
        seen.add(text.casefold())
        jobs.setdefault(str(row.get("job") or ""), []).append((index, text))
    ordered = []
    for turn in range(max((len(rows) for rows in jobs.values()), default=0)):
        ordered += [rows[turn] for rows in jobs.values() if turn < len(rows)]
    return ordered


def _names(values, limit: int, *, exclude: str = "") -> list[str]:
    names = []
    for value in values or []:
        text = " ".join(str(value).split())
        if 2 <= len(text) <= 100 and text.casefold() not in {n.casefold() for n in names}:
            if text.casefold() != exclude.casefold():
                names.append(text)
    return names[:limit]


def request_inputs(
    panel: dict, scope: dict, policy: dict, prompts: list[str], max_cost_usd: Decimal
) -> dict:
    """The JSON inputs ai_answers.AIAnswersRequest.from_inputs validates."""
    name = " ".join(str(panel.get("name") or "").split())[:100]
    return {
        "prompts": prompts,
        "engines": list(policy["ai_engines"]),
        "brand": {
            "name": name,
            "domain": scope["host"],
            "aliases": _names(panel.get("aliases"), ai_answers.LIMITS["aliases"], exclude=name),
            "competitors": _names(
                panel.get("competitor_names"), ai_answers.LIMITS["competitors"], exclude=name
            ),
        },
        "max_cost_usd": str(max_cost_usd),
        "market": scope["market"],
        "language_code": scope.get("language", "en"),
        "priority": policy["ai_engines_priority"],
        "deadline_seconds": policy["ai_engines_deadline_seconds"],
    }


def results(plan: dict | None, measured: dict | None) -> dict:
    """What the audit reports: the plan, and per engine and per answer what came back."""
    if plan is None:
        return {"status": "not_measured", "reason": "not_started"}
    if not plan.get("stage"):
        return plan
    if measured is None:
        return {**plan, "status": "not_measured", "reason": "measurement_unavailable"}
    summary = measured.get("summary") or {}
    if summary.get("status") == "rejected":
        return {**plan, "status": "not_measured", "reason": "cost_ceiling"}
    order = plan["question_indexes"]
    rows = [
        {
            "question": order[row["prompt_index"]] + 1,
            "engine": row["engine"],
            "measurement": row["measurement"],
            "status": row["status"],
            "reason": row.get("reason"),
            "model": row.get("model"),
            **{key: bool(row["brand"][key]) for key in ("mentioned", "cited", "recommended_first")},
            "cost_usd": row.get("cost_usd"),
            "cited_urls": row.get("cited_urls", [])[:MAX_CITED_URLS],
            "answer": row.get("answer", ""),
            "answer_truncated": row.get("answer_truncated", False),
        }
        for row in measured.get("rows") or []
    ]
    by_engine = []
    for engine in plan["engines"]:
        counts = (summary.get("by_engine") or {}).get(engine) or {}
        models = [r["model"] for r in rows if r["engine"] == engine and r["model"]]
        by_engine.append(
            {
                "engine": engine,
                "label": LABELS.get(engine, engine),
                "measurement": ai_answers.ENGINES[engine].measurement,
                "model": models[0] if models else ai_answers.ENGINES[engine].model,
                "asked": plan["asked"],
                **{
                    key: counts.get(key, 0)
                    for key in (
                        "answered",
                        "no_answer",
                        "failed",
                        "timeout",
                        "unknown",
                        "mentioned",
                        "cited",
                        "recommended_first",
                    )
                },
                "cost_usd": counts.get("cost_usd", "0"),
                "cost_unconfirmed_rows": counts.get("cost_unconfirmed_rows", 0),
            }
        )
    return {
        **plan,
        "status": "measured" if summary.get("complete") else "partial",
        "reason": None,
        "cost_usd": summary.get("cost_usd"),
        "cost_unconfirmed_rows": summary.get("cost_unconfirmed_rows", 0),
        "by_engine": by_engine,
        "rows": rows,
    }


def _money(value) -> str:
    try:
        return f"${Decimal(str(value)).quantize(Decimal('0.0001'))}"
    except ArithmeticError:
        return "unknown"


def report_lines(engines: dict | None) -> list[str]:
    """The AUDIT.md section: per engine, apps and API models in separate tables."""
    engines = engines or results(None, None)
    lines = ["### Answers across AI engines", ""]
    if engines.get("status") == "not_measured":
        reason = REASONS.get(engines.get("reason"), REASONS["not_started"])
        return [*lines, f"Not measured. {reason}", ""]
    asked, questions = engines["asked"], engines["questions"]
    lines += [
        f"Tin asked {asked} of the {questions} buyer questions above on "
        f"{len(engines['engines'])} AI engines through DataForSEO, within a "
        f"{_money(engines['max_cost_usd'])} ceiling; they cost {_money(engines['cost_usd'])}"
        + (
            f", with {engines['cost_unconfirmed_rows']} answers' cost unconfirmed."
            if engines.get("cost_unconfirmed_rows")
            else "."
        )
        + " Each count is out of the questions asked; an answer an engine did not give, or "
        "Tin could not read, counts as neither yes nor no.",
        "",
    ]
    for measurement, (title, note) in MEASUREMENTS.items():
        mine = [row for row in engines["by_engine"] if row["measurement"] == measurement]
        if not mine:
            continue
        lines += [
            f"{title} ({measurement}): {note}",
            "",
            "| Engine | Answered | Mentioned | Cited your site | Named first | Cost |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
            *(
                f"| {row['label']}"
                + (f" ({row['model']})" if row["model"] else "")
                + f" | {row['answered']}/{asked} | {row['mentioned']} | {row['cited']} | "
                f"{row['recommended_first']} | {_money(row['cost_usd'])}"
                + (
                    f" and {row['cost_unconfirmed_rows']} unconfirmed |"
                    if row["cost_unconfirmed_rows"]
                    else " |"
                )
                for row in mine
            ),
            "",
        ]
    skipped = engines.get("not_asked") or []
    if skipped:
        lines += [
            "Not asked, to stay within the ceiling: "
            + ", ".join(f"Q{index + 1}" for index in skipped)
            + ".",
            "",
        ]
    missing: dict[tuple[str, str], list[int]] = {}
    for row in engines["rows"]:
        if row["status"] in ROW_STATUSES:
            missing.setdefault((row["engine"], row["status"]), []).append(row["question"])
    if missing:
        lines += [
            "Answers missing:",
            "",
            *(
                f"- {LABELS.get(engine, engine)}, "
                + ", ".join(f"Q{number}" for number in sorted(numbers))
                + f": {ROW_STATUSES[status]}."
                for (engine, status), numbers in missing.items()
            ),
            "",
        ]
    lines += ["The answers and the URLs they cite are in `evidence.json`.", ""]
    return lines


def headline(engines: dict | None) -> dict:
    """The SUMMARY.json block: one row per engine, no answers or URLs."""
    engines = engines or results(None, None)
    return {
        "status": engines.get("status"),
        "reason": engines.get("reason"),
        "questions": engines.get("questions"),
        "asked": engines.get("asked"),
        "max_cost_usd": engines.get("max_cost_usd"),
        "cost_usd": engines.get("cost_usd"),
        "columns": list(ENGINE_COLUMNS),
        "rows": [
            [row[column] for column in ENGINE_COLUMNS] for row in engines.get("by_engine", [])
        ],
        "note": "measurement is consumer_app_answer (what a person sees in the ChatGPT or Gemini "
        "app, Google AI Mode or an AI Overview) or api_model_answer (Claude or Perplexity's API "
        "model with web search, not claude.ai or perplexity.ai). Counts are answers out of "
        "asked; an answer not given or not read counts as neither.",
    }
