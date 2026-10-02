"""organic-audit-v13 asks its buyer questions on six AI engines; offline only.

The measurement is ai_answers (DataForSEO) behind the trusted ai_answers_measure activity.
Here a synthetic DataForSEO answers; no provider, model or Temporal server is used.
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from temporalio.testing import ActivityEnvironment
from test_ai_answers import Clock, FakeDataForSEO, client_for
from test_organic_audit import activities_fixture

from tin_lite.ai_answers import API_MODEL_ANSWER, CONSUMER_APP_ANSWER, AIAnswersRequest
from tin_lite.ai_answers_activities import AIAnswersActivities, receipt_key
from tin_lite.organic_audit import (
    AUDIT_POLICY,
    LATEST_SUMMARY_PATH,
    SUMMARY_READ_LIMIT,
    V12_AUDIT_POLICY,
    audit_paths,
    summary_paths,
)
from tin_lite.organic_audit_engines import (
    ENGINE_COLUMNS,
    LABELS,
    headline,
    per_question_usd,
    question_order,
    report_lines,
    results,
)
from tin_lite.service_pricing import NANOS_PER_DOLLAR, service_terms

JOBS = ("Track team deadlines", "Plan a product launch")
FAMILIES = ("discovery", "problem", "comparison", "constraint")


def panel(questions=8):
    rows = [
        {
            "job": JOBS[index % 2],
            "family": FAMILIES[index // 2 % 4],
            "question": f"Which tool should a small team use for question number {index + 1}?",
            "fit_reason": "fixture",
            "source_url": "https://example.com/",
        }
        for index in range(questions)
    ]
    # The panel lists one job's questions, then the other's, as a drafted panel does.
    rows.sort(key=lambda row: JOBS.index(row["job"]))
    return {
        "status": "completed",
        "site_type": "product",
        "host": "example.com",
        "name": "Acme Forms",
        "aliases": ["Acme", "acme forms"],
        "competitor_names": ["Formly"],
        "questions": rows,
        "repetitions": 3,
        "unsearched": True,
        "planned_observations": questions * 4,
        "sha256": "f" * 64,
    }


async def audit(*, budget="8", policy=None, questions=8):
    activities, db, storage, provider = await activities_fixture(budget=budget, policy=policy)
    run_id = str(db.run.id)
    await activities._save(run_id, "panel", panel(questions))
    return activities, db, storage, run_id


def test_the_policy_asks_six_engines_within_a_dollar():
    assert AUDIT_POLICY["ai_engines"] == list(LABELS)
    assert per_question_usd(AUDIT_POLICY) == Decimal("0.0776")
    assert per_question_usd(AUDIT_POLICY) * AUDIT_POLICY["max_questions"] <= Decimal(
        AUDIT_POLICY["ai_engines_max_cost_usd"]
    )
    # The audit's billing maximum grows by the engines' ceiling, for v13 runs only.
    definition = {"executor": "organic.audit"}
    v12 = service_terms({**definition, "audit_policy": V12_AUDIT_POLICY})
    v13 = service_terms({**definition, "audit_policy": AUDIT_POLICY})
    assert v13["maximum_nanos"] - v12["maximum_nanos"] == NANOS_PER_DOLLAR


def test_questions_alternate_between_jobs_so_a_ceiling_keeps_both():
    ordered = question_order(panel())
    assert [index for index, _ in ordered] == [0, 4, 1, 5, 2, 6, 3, 7]
    assert len({text for _, text in ordered}) == 8


@pytest.mark.asyncio
async def test_v13_saves_the_questions_for_six_engines_once():
    activities, db, _, run_id = await audit()
    assert await activities.organic_prepare_ai_engines(run_id) == "engines"
    plan = await activities._result(run_id, "ai_engines")
    assert plan["asked"] == 8 and plan["not_asked"] == []
    assert plan["max_cost_usd"] == "0.6208"
    saved = db.effects[receipt_key(run_id, "engines", "request")].result
    request = AIAnswersRequest.from_inputs(saved)
    assert request.engines == tuple(AUDIT_POLICY["ai_engines"])
    assert request.brand.name == "Acme Forms" and request.brand.domain == "example.com"
    assert request.brand.aliases == ("Acme",)  # A repeat of the name is not an alias.
    assert request.brand.competitors == ("Formly",)
    assert request.max_cost_usd == Decimal("0.6208")
    budget = db.effects[f"organic:{run_id}:budget"].result
    assert budget["ai_engines"] == "0.6208"
    # A retry neither reserves nor saves again.
    assert await activities.organic_prepare_ai_engines(run_id) == "engines"
    assert db.effects[f"organic:{run_id}:budget"].result == budget


@pytest.mark.asyncio
async def test_the_ceiling_drops_questions_not_engines():
    activities, db, _, run_id = await audit(budget="0.30")
    assert await activities.organic_prepare_ai_engines(run_id) == "engines"
    plan = await activities._result(run_id, "ai_engines")
    # $0.30 left buys three questions on all six engines; both jobs are still asked.
    assert plan["asked"] == 3 and plan["question_indexes"] == [0, 4, 1]
    assert plan["not_asked"] == [2, 3, 5, 6, 7]
    request = db.effects[receipt_key(run_id, "engines", "request")].result
    assert request["max_cost_usd"] == "0.2328" and len(request["engines"]) == 6


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("budget", "questions", "reason"),
    [("0.05", 8, "cost_ceiling"), ("8", 0, "no_question_panel")],
)
async def test_nothing_to_ask_is_saved_with_its_reason(budget, questions, reason):
    activities, db, _, run_id = await audit(budget=budget, questions=questions)
    assert await activities.organic_prepare_ai_engines(run_id) is None
    plan = await activities._result(run_id, "ai_engines")
    assert plan["status"] == "not_measured" and plan["reason"] == reason
    assert receipt_key(run_id, "engines", "request") not in db.effects
    assert (
        "ai_engines"
        not in (db.effects.get(f"organic:{run_id}:budget") or SimpleNamespace(result={})).result
    )


@pytest.mark.asyncio
async def test_a_v12_run_asks_no_engine():
    activities, db, _, run_id = await audit(policy=V12_AUDIT_POLICY)
    assert await activities.organic_prepare_ai_engines(run_id) is None
    assert await activities._result(run_id, "ai_engines") is None


async def measured_audit(fake):
    activities, db, storage, run_id = await audit()
    await activities.organic_start_crawl(run_id)
    assert await activities.organic_poll_crawl(run_id)
    await activities.organic_end_crawl(run_id)
    stage = await activities.organic_prepare_ai_engines(run_id)
    clock = Clock()
    answers = AIAnswersActivities(
        database=db,
        settings=SimpleNamespace(),
        client=client_for(fake),
        clock=clock,
        sleep=clock.sleep,
    )
    summary = await ActivityEnvironment().run(
        answers.ai_answers_measure, {"run_id": run_id, "stage": stage}
    )
    await activities.organic_publish(run_id)
    tree = storage.repo.trees[storage.repo.head]
    return run_id, summary, {path: value[1] for path, value in tree.items()}


@pytest.mark.asyncio
async def test_the_report_and_summary_keep_apps_and_api_models_apart():
    fake = FakeDataForSEO()
    fake.never_ready = False
    fake.live_error = {"perplexity"}  # One engine's outcome is unknown; it is not resent.
    run_id, summary, files = await measured_audit(fake)
    assert summary["status"] == "measured" and summary["rows"] == 48
    report = files[audit_paths(run_id)["AUDIT.md"]].decode()
    section = report[report.index("### Answers across AI engines") :]
    apps = section.index(f"In the apps people use ({CONSUMER_APP_ANSWER})")
    api = section.index(f"From API models ({API_MODEL_ANSWER})")
    assert apps < section.index("| ChatGPT app") < api < section.index("| Claude API model")
    assert "| Perplexity API model (sonar) | 0/8 | 0 | 0 | 0 | $0.0000 and 8 unconfirmed |" in (
        section
    )
    assert (
        "- Perplexity API model, Q1, Q2, Q3, Q4, Q5, Q6, Q7, Q8: the outcome could not be "
        "confirmed and was not bought again." in section
    )
    assert "Tin asked 8 of the 8 buyer questions above on 6 AI engines" in section

    raw = files[summary_paths(run_id)["SUMMARY.json"]]
    assert len(raw) <= SUMMARY_READ_LIMIT and files[LATEST_SUMMARY_PATH] == raw
    engines = json.loads(raw)["ai_engines"]
    rows = {row[0]: dict(zip(engines["columns"], row, strict=True)) for row in engines["rows"]}
    assert engines["columns"] == list(ENGINE_COLUMNS) and engines["asked"] == 8
    assert {k: rows[k]["measurement"] for k in rows} == {
        "chatgpt": CONSUMER_APP_ANSWER,
        "gemini": CONSUMER_APP_ANSWER,
        "google_ai_mode": CONSUMER_APP_ANSWER,
        "google_ai_overview": CONSUMER_APP_ANSWER,
        "claude": API_MODEL_ANSWER,
        "perplexity": API_MODEL_ANSWER,
    }
    assert rows["chatgpt"]["answered"] == 8 and rows["chatgpt"]["mentioned"] == 8
    assert rows["perplexity"]["answered"] == 0
    assert "acmeforms" not in raw.decode()  # Counts only; answers and URLs stay in evidence.

    evidence = json.loads(files[audit_paths(run_id)["evidence.json"]])
    kept = evidence["ai_visibility"]["engines"]
    assert kept["status"] == "partial" and len(kept["rows"]) == 48
    first = next(r for r in kept["rows"] if r["engine"] == "chatgpt")
    assert first["question"] == 1 and first["answer"].startswith("1. **Acme Forms**")


@pytest.mark.asyncio
async def test_a_measurement_that_never_finished_is_reported_not_guessed():
    activities, db, storage, run_id = await audit()
    await activities.organic_start_crawl(run_id)
    assert await activities.organic_poll_crawl(run_id)
    await activities.organic_end_crawl(run_id)
    assert await activities.organic_prepare_ai_engines(run_id) == "engines"
    await activities.organic_publish(run_id)  # ai_answers_measure failed before saving.
    tree = storage.repo.trees[storage.repo.head]
    report = tree[audit_paths(run_id)["AUDIT.md"]][1].decode()
    assert "Not measured. The measurement did not finish." in report
    engines = json.loads(tree[summary_paths(run_id)["SUMMARY.json"]][1])["ai_engines"]
    assert engines["status"] == "not_measured" and engines["rows"] == []
    assert engines["reason"] == "measurement_unavailable"


def test_without_a_plan_the_section_says_so():
    assert report_lines(None)[2] == "Not measured. The measurement was not started."
    assert headline(results(None, None))["status"] == "not_measured"
