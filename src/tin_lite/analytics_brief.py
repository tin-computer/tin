"""The analytics-brief.v1 output check: what a product analytics brief says it measured.

The product analytics procedure ends its report with one JSON evidence block (REPORT.md).
Tin reads the report's Status line and that evidence before publishing. A brief that
measured nothing fails its run and is kept only as a saved diagnostic; any other
incomplete brief is published with a summary that says why. The figures themselves are
never recomputed here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

VALIDATOR = "analytics-brief.v1"
MEASURED_NOTHING = "AnalyticsBriefMeasuredNothing"
MARKER = "<!-- tin-analytics-evidence-v1 -->"
STATUSES = frozenset(
    {"complete", "incomplete", "invalid configuration", "unsupported exclusions", "schema changed"}
)
OUTCOMES = frozenset({"ok", "refused", "invalid"})
OPERATIONS = frozenset({"query.hogql", "property_definitions.list"})
FIELDS = frozenset(
    {
        "status",
        "status_reason",
        "provider",
        "binding",
        "generated_at",
        "windows",
        "state",
        "inventory_scope",
        "requests",
        "derived",
        "limitations",
    }
)
MAX_REASON = 180
_STATUS = re.compile(r"^(?:[-*] )?Status: (.*?)[ \t]*$", re.MULTILINE)
_EVIDENCE = re.compile(r"\A\s*```json\n(.+?)\n```\s*\Z", re.DOTALL)


@dataclass(frozen=True)
class Outcome:
    status: str
    reason: str
    # A plain reason when the brief measured nothing; such a brief is never published.
    measured_nothing: str | None = None


def read(content: bytes | str) -> Outcome:
    """Parse and check one report. Raises ValueError when it breaks the REPORT.md contract."""
    text = content.decode("utf-8") if isinstance(content, bytes) else content
    text = text.replace("\r\n", "\n")
    if text.count(MARKER) != 1:
        raise ValueError("analytics brief must end with one evidence block")
    brief, tail = text.split(MARKER)
    statuses = _STATUS.findall(brief)
    if len(statuses) != 1 or statuses[0] not in STATUSES:
        raise ValueError("analytics brief needs one Status line with a REPORT.md status")
    status = statuses[0]
    match = _EVIDENCE.fullmatch(tail)
    if match is None:
        raise ValueError("analytics evidence must be one fenced JSON block")
    try:
        evidence = json.loads(match.group(1))
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("analytics evidence is not valid JSON") from exc
    if not isinstance(evidence, dict) or not FIELDS <= set(evidence):
        raise ValueError("analytics evidence is missing REPORT.md fields")
    if evidence["status"] != status or evidence["provider"] != "analytics.posthog":
        raise ValueError("analytics evidence does not match the report header")
    reason = evidence["status_reason"]
    if (
        not isinstance(reason, str)
        or len(reason) > MAX_REASON
        or "\n" in reason
        or (status == "complete") == bool(reason.strip())
    ):
        raise ValueError("an incomplete analytics brief needs a short status_reason")
    requests = _requests(evidence["requests"])
    scope = _scope(evidence["inventory_scope"])
    derived = evidence["derived"]
    if not isinstance(derived, dict) or not isinstance(evidence["limitations"], list):
        raise ValueError("analytics evidence derived/limitations are invalid")
    return Outcome(status, reason.strip(), _measured_nothing(status, requests, scope, derived))


def validate(content: bytes | str) -> None:
    read(content)


def summary(outcome: Outcome) -> str | None:
    """The run summary for an incomplete brief; None keeps the procedure's own summary."""
    if outcome.status == "complete":
        return None
    reason = outcome.reason.rstrip(".")
    if outcome.status == "incomplete":
        return f"Analytics brief incomplete: {reason}."
    return f"Analytics brief incomplete ({outcome.status}): {reason}."


def failure(outcome: Outcome) -> str:
    return (
        f"The analytics brief measured nothing: {outcome.measured_nothing}. "
        "Its diagnostic is saved with this run and was not added to Files."
    )


def _requests(value) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("analytics evidence requests must be a list")
    for item in value:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("step"), str)
            or item.get("operation") not in OPERATIONS
            or item.get("outcome") not in OUTCOMES
        ):
            raise ValueError("analytics evidence has an invalid request record")
    return value


def _scope(value) -> dict | None:
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or type(value.get("total_event_types")) is not int
        or type(value.get("returned_event_types")) is not int
        or type(value.get("complete")) is not bool
        or not 0 <= value["returned_event_types"] <= value["total_event_types"]
    ):
        raise ValueError("analytics evidence inventory_scope is invalid")
    return value


def _measured_nothing(status, requests, scope, derived) -> str | None:
    queries = [r for r in requests if r["operation"] == "query.hogql"]
    succeeded = {r["step"] for r in queries if r["outcome"] == "ok"}
    if "inventory" in succeeded:
        if scope is None:
            raise ValueError("analytics evidence omits the inventory scope")
        if scope["total_event_types"] == 0:
            return "PostHog returned no events in the 90-day lookback"
    # Saved-input diagnostics make no queries; a brief that claims completion must have some.
    if not succeeded and (queries or status == "complete"):
        return "every analytics query was refused or failed; the saved diagnostic lists each one"
    if "coverage" in succeeded:
        rows = derived.get("coverage")
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or type(row.get("raw")) is not int or row["raw"] < 0
            for row in rows
        ):
            raise ValueError("analytics evidence omits the coverage rows")
        if not sum(row["raw"] for row in rows):
            return "none of the selected events occurred in either reporting window"
    return None
