"""Offline checks for the growth.churn_signals package: synthetic data, no provider calls."""

import json
import runpy
from datetime import date, timedelta
from types import SimpleNamespace

import jsonschema
import pytest

from tin_lite.code_models import request_contract
from tin_lite.community import REPOSITORY_ROOT
from tin_lite.workflow_code import validate_code_definition, validate_code_result

ROOT = REPOSITORY_ROOT / "workflow_packages" / "growth.churn_signals"
DEFINITION = json.loads((ROOT / "workflow.json").read_text())["definition"]
SPEC = validate_code_definition(DEFINITION)
MODULE = SimpleNamespace(**runpy.run_path(str(ROOT / "main.py")))

CUSTOMERS = """customer_id,plan,mrr,status
c1,Pro,200,active
c2,Pro,200,active
c3,Pro,150,active
c4,Pro,150,active
c5,Starter,40,active
c6,Starter,40,active
c7,Starter,40,active
c8,Starter,40,active
c9,Pro,200,cancelled
c10,Starter,40,cancelled
"""

FEEDBACK = """date,customer_id,source,text
2026-06-01,c8,ticket,Old ticket: the app crashed on upload once
2026-07-20,c1,ticket,The iOS app crashes when I upload a photo
2026-08-19,c1,ticket,iOS app crashed again during upload
2026-08-21,c2,ticket,Upload crashes the iPhone app every time
2026-08-24,c3,review,Crashes on upload. We are switching to another tool if this continues
2026-08-26,c4,ticket,iOS upload crash is blocking my team
2026-08-28,c9,ticket,Cancelled because upload kept crashing on iOS
2026-08-29,,review,App crashes on upload on my iPhone
2026-07-10,c5,ticket,CSV export takes forever on large projects
2026-07-25,c6,ticket,Exporting to CSV is very slow
2026-08-05,c10,ticket,CSV export is too slow for us
2026-08-25,c5,ticket,CSV export still slow
2026-08-27,c7,review,Love the new dashboard
2026-08-27,c7,review,Love the new dashboard
"""

CHANGELOG = """v2.4 (Aug 30)
- Faster CSV exports: large exports now finish in seconds.
- New dashboard filters.
"""

BASE = {
    "feedback_csv": FEEDBACK,
    "customers_csv": CUSTOMERS,
    "changelog": CHANGELOG,
    "as_of": "2026-09-01",
}


def tag_row(row):
    text = row["text"].lower()
    if "crash" in text:
        return {
            "id": row["id"],
            "is_complaint": True,
            "issue": "iOS upload crash",
            "category": "bug",
            "severity": "critical",
            "churn_intent": "switching" in text or "cancelled" in text,
        }
    if "csv" in text:
        return {
            "id": row["id"],
            "is_complaint": True,
            "issue": "slow_csv_export",
            "category": "performance",
            "severity": "medium",
            "churn_intent": False,
        }
    return {
        "id": row["id"],
        "is_complaint": False,
        "issue": "none",
        "category": "other",
        "severity": "low",
        "churn_intent": False,
    }


def plan_row(risk, *, evidence=True):
    fixed = risk["issue"] == "slow_csv_export"
    return {
        "risk_id": risk["risk_id"],
        "title": "Slow CSV exports" if fixed else "iOS app crashes on upload",
        "changelog_status": "fixed" if fixed else "open",
        "changelog_evidence": (
            (
                "Faster CSV exports: large exports now finish in seconds."
                if evidence
                else "CSV exports rewritten in v3"
            )
            if fixed
            else ""
        ),
        "acknowledge_note": f"We know about the {risk['issue']} problem and are working on it.",
        "fixed_note": "Large CSV exports now finish in seconds." if fixed else "",
        "win_back_note": (
            "Exports are fast now. We would love to have you back."
            if fixed and risk["has_cancelled_customers"]
            else ""
        ),
    }


def context(tagger=None, planner=None):
    calls = []

    async def generate(**payload):
        request_contract(SPEC, payload)  # route, step and serialized input size
        calls.append(payload)
        if payload["step"] == "tag_feedback":
            output = (tagger or (lambda rows: {"items": [tag_row(r) for r in rows]}))(
                payload["data"]
            )
        else:
            assert payload["step"] == "plan_risk_moves"
            output = (planner or (lambda data: {"risks": [plan_row(r) for r in data["risks"]]}))(
                payload["data"]
            )
        jsonschema.validate(output, payload["output_schema"])  # plausible: passes the schema
        return {"parsed": output, "text": json.dumps(output)}

    return SimpleNamespace(models=SimpleNamespace(generate=generate)), calls


async def test_ranks_rising_risk_first_and_assigns_one_move_per_customer():
    ctx, calls = context()
    result = await MODULE.run(ctx, dict(BASE))
    validate_code_result(json.dumps(result).encode(), SPEC)
    content = result["content"]

    assert [c["step"] for c in calls] == ["tag_feedback", "plan_risk_moves"]
    # The duplicate praise row and the June row never reach the model.
    assert len(calls[0]["data"]) == 12
    assert "1 feedback row outside the windows" in content
    assert "1 feedback row skipped" in content

    risks = content.split("## Ranked risks")[1].split("### R1")[0]
    # Six recent crash mentions against 1 baseline mention × 14/42 days expected.
    assert "| R1 | iOS app crashes on upload | rising (6 vs 0.3) | 5 | $850 |" in risks
    assert "| R2 | Slow CSV exports | steady (1 vs 1.0) | 2 | $80 |" in risks
    # $850 = c1-c4 ($700) + the anonymous reviewer at the median MRR ($150).
    # c9 cancelled, so its MRR counts as lost, not at risk.
    assert "Concentrated on the **Pro** plan" in content

    table = content.split("## Who to contact")[1]
    assert "| c1 | Pro | $200 | R1 | Acknowledge before they cancel |" in table
    assert "| c5 | Starter | $40 | R2 | Tell them it's fixed |" in table
    assert "| c10 | Starter | $40 | R2 | Win back |" in table
    assert "| c9 " not in table and "1 cancelled customer held until" in table
    assert "anonymous" not in table


async def test_tagging_with_unknown_ids_is_rejected_before_planning():
    def tagger(rows):
        tags = [tag_row(r) for r in rows]
        tags[-1]["id"] = tags[0]["id"]  # plausible shape, duplicated ID
        return {"items": tags}

    ctx, calls = context(tagger=tagger)
    with pytest.raises(ValueError, match="exactly once"):
        await MODULE.run(ctx, dict(BASE))
    assert len(calls) == 1


async def test_tagging_with_too_many_issues_is_rejected():
    def tagger(rows):
        tags = [tag_row(r) for r in rows]
        for n, tag in enumerate(tags):
            tag.update(is_complaint=True, issue=f"issue_number_{n}")
        return {"items": tags}

    rows = "\n".join(f"2026-08-{10 + n},c{n},ticket,Problem {n}" for n in range(16))
    ctx, _ = context(tagger=tagger)
    with pytest.raises(ValueError, match="too many distinct issues"):
        await MODULE.run(ctx, {**BASE, "feedback_csv": "date,customer_id,source,text\n" + rows})


async def test_plan_with_an_invented_risk_is_rejected():
    def planner(data):
        risks = [plan_row(r) for r in data["risks"]]
        risks[0]["risk_id"] = "R9"
        return {"risks": risks}

    ctx, _ = context(planner=planner)
    with pytest.raises(ValueError, match="every supplied risk ID"):
        await MODULE.run(ctx, dict(BASE))


async def test_claimed_fix_missing_from_changelog_is_treated_as_open():
    ctx, _ = context(
        planner=lambda data: {"risks": [plan_row(r, evidence=False) for r in data["risks"]]}
    )
    content = (await MODULE.run(ctx, dict(BASE)))["content"]
    assert "R2: claimed fixed without matching changelog text; treated as open" in content
    assert "Tell them it's fixed" not in content.split("## Who to contact")[1]
    assert "| c10 " not in content.split("## Who to contact")[1]  # held until really fixed


async def test_offer_only_appears_when_the_founder_supplied_one():
    def planner(data):
        risks = [plan_row(r) for r in data["risks"]]
        for risk in risks:
            if risk["win_back_note"]:
                risk["win_back_note"] = "Come back and get 20% off for three months."
        return {"risks": risks}

    ctx, _ = context(planner=planner)
    with pytest.raises(ValueError, match="offer that was not supplied"):
        await MODULE.run(ctx, dict(BASE))
    ctx, _ = context(planner=planner)
    result = await MODULE.run(ctx, {**BASE, "winback_offer": "20% off for three months"})
    assert "20% off" in result["content"]


async def test_placeholder_in_a_draft_is_rejected():
    def planner(data):
        risks = [plan_row(r) for r in data["risks"]]
        risks[0]["acknowledge_note"] = "Hi [Name], we are on it."
        return {"risks": risks}

    ctx, _ = context(planner=planner)
    with pytest.raises(ValueError, match="placeholder"):
        await MODULE.run(ctx, dict(BASE))


async def test_no_complaints_skips_the_plan_step():
    ctx, calls = context(
        tagger=lambda rows: {
            "items": [{**tag_row(r), "is_complaint": False, "issue": "none"} for r in rows]
        }
    )
    result = await MODULE.run(ctx, {"feedback_csv": FEEDBACK, "as_of": "2026-09-01"})
    validate_code_result(json.dumps(result).encode(), SPEC)
    assert len(calls) == 1
    assert "No complaints were found" in result["content"]
    assert "ranking uses customer counts" in result["content"]


async def test_large_exports_are_thinned_evenly_and_fit_the_route():
    start = date(2026, 7, 7)
    rows = [
        f"{(start + timedelta(days=n % 56)).isoformat()},c{n},ticket,"
        + f"CSV export is slow on project {n} " * 6
        for n in range(300)
    ]
    ctx, calls = context()
    await MODULE.run(
        ctx,
        {"feedback_csv": "date,customer_id,source,text\n" + "\n".join(rows), "as_of": "2026-09-01"},
    )
    sent = calls[0]["data"]
    assert len(sent) <= MODULE.MAX_TAGGED
    # Even thinning keeps roughly a quarter of the sample in the 14-of-56-day recent window.
    recent = [r for r in sent if int(r["text"].split("project ")[1].split()[0]) % 56 >= 42]
    assert 0.15 < len(recent) / len(sent) < 0.35


@pytest.mark.parametrize(
    "feedback",
    ["customer_id,text\nc1,hello\n", "date,text\nnot-a-date,hello\n", ""],
)
async def test_unusable_feedback_is_rejected_before_any_model_call(feedback):
    ctx, calls = context()
    with pytest.raises(ValueError):
        await MODULE.run(ctx, {"feedback_csv": feedback})
    assert not calls
