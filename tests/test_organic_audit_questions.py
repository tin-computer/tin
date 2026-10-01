"""organic-audit-v10 keeps one buyer-question set per site and market; offline, no paid calls."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from test_organic_audit import activities_fixture, panel_fixture, response
from test_organic_audit_panel import interpretation, research, review

from tin_lite.organic_audit import (
    AUDIT_POLICY,
    V9_AUDIT_POLICY,
    audit_paths,
    content_review_findings,
    question_results,
)
from tin_lite.organic_audit_ai import summarize
from tin_lite.organic_audit_panel import limit_panel

JOBS = ["Planning team work", "Tracking team goals", "Running team reviews"]


def three_job_panel():
    base = panel_fixture()
    questions = [
        {
            **question,
            "job": job,
            "question": question["question"].rstrip("?") + f" when {job.lower()}?",
        }
        for job in JOBS
        for question in base["questions"]
    ]
    return {**base, "questions": questions}


def draft_response(panel):
    return response(json.dumps(panel), search=False)


def answer(text="Several planning tools can help small teams."):
    return response(text)


def judge(mentioned):
    quote = "Acme is a good fit." if mentioned else ""
    return response(
        json.dumps(
            {
                "mentioned": mentioned,
                "mention_quote": quote,
                "evaluated": mentioned,
                "evaluation_quote": quote,
                "shortlisted": mentioned,
                "shortlist_quote": quote,
                "selected_first": False,
                "first_choice_quote": "",
            }
        ),
        search=False,
    )


async def draft_first_panel(activities, run_id):
    calls = [
        research(),
        draft_response(three_job_panel()),
        *[interpretation() for _ in range(8)],
        review(),
    ]
    activities.responses = SimpleNamespace(create=AsyncMock(side_effect=calls))
    return await activities.organic_prepare_panel(run_id)


def test_limit_keeps_the_first_jobs_and_recomputes_the_question_set_digest():
    from test_organic_audit_results import frozen_panel

    panel = frozen_panel()
    limited = limit_panel(panel, max_jobs=2, repetitions=3)
    assert limited["questions"] == panel["questions"]
    assert limited["planned_observations"] == 12 and limited["repetitions"] == 3
    assert limited["sha256"] == panel["sha256"]


@pytest.mark.asyncio
async def test_first_audit_drafts_two_buyer_jobs_with_three_answers_each():
    activities, db, _, _ = await activities_fixture()
    run_id = str(db.run.id)
    # Eight questions, three answers each with web search and one without: 32.
    assert await draft_first_panel(activities, run_id) == 32
    panel = await activities._result(run_id, "panel")
    assert panel["repetitions"] == 3 and panel["planned_observations"] == 32
    assert panel["unsearched"] is True
    assert list(dict.fromkeys(q["job"] for q in panel["questions"])) == JOBS[:2]
    requests = [call.args[0] for call in activities.responses.create.await_args_list]
    # Only the eight kept questions were interpreted; the third job was never reviewed.
    assert len(requests) == 11
    review_input = json.loads(requests[-1]["input"])
    assert len(review_input["standalone_questions"]) == 8
    assert await activities._result(run_id, "panel_baseline") is None


@pytest.mark.asyncio
async def test_each_question_gets_three_answers_in_order():
    activities, db, _, _ = await activities_fixture()
    run_id = str(db.run.id)
    await draft_first_panel(activities, run_id)
    panel = await activities._result(run_id, "panel")
    activities.responses = SimpleNamespace(create=AsyncMock(return_value=answer()))
    for index in range(6):
        await activities.organic_observe({"run_id": run_id, "index": index})
    sent = [call.args[0]["input"] for call in activities.responses.create.await_args_list]
    assert sent == [panel["questions"][0]["question"]] * 3 + [panel["questions"][1]["question"]] * 3
    rows = [await activities._result(run_id, f"observation:{index}") for index in range(6)]
    assert [(row["question_index"], row["repetition"]) for row in rows] == [
        (0, 1),
        (0, 2),
        (0, 3),
        (1, 1),
        (1, 2),
        (1, 3),
    ]


def previous_audit(db, *, market="US", host="example.com", repetitions=3, mentioned=()):
    """Receipts of an earlier published audit in the same project."""
    from test_organic_audit_results import frozen_panel

    source = SimpleNamespace(id=uuid4())
    panel = frozen_panel()
    if repetitions:
        panel = limit_panel(panel, max_jobs=2, repetitions=repetitions, unsearched=True)
    panel = {"status": "completed", **panel}
    effects = {
        "scope": {
            "host": host,
            "market": market,
            "started_at": "2026-09-01T00:00:00+00:00",
            "policy_version": AUDIT_POLICY["version"],
        },
        "panel": panel,
    }
    reps = repetitions or 2
    for index in range(len(panel["questions"]) * reps):
        effects[f"observation:{index}"] = {
            "status": "completed",
            "index": index,
            "question_index": index // reps,
            "repetition": index % reps + 1,
            "classification": {
                "mentioned": index in mentioned,
                "owned_domain_cited": False,
                "shortlisted": index in mentioned,
                "selected_first": False,
            },
        }
    from tin_lite.domain import EffectReceipt

    for stage, result in effects.items():
        key = f"organic:{source.id}:{stage}"
        db.effects[key] = EffectReceipt(key, "organic.audit", "completed", result)
    db.list_prerequisite_runs = AsyncMock(return_value=[("organic.audit", source)])
    return source, panel


@pytest.mark.asyncio
async def test_a_later_audit_reuses_the_frozen_questions_without_paid_preparation():
    activities, db, storage, _ = await activities_fixture()
    run_id = str(db.run.id)
    source, panel = previous_audit(db, mentioned={0})
    activities.responses = SimpleNamespace(create=AsyncMock(side_effect=AssertionError))
    assert await activities.organic_prepare_panel(run_id) == 16
    assert await activities._result(run_id, "panel") == panel
    preparation = await activities._result(run_id, "panel_preparation")
    assert preparation["method"] == "reused_frozen_panel"
    assert preparation["source_run_id"] == str(source.id)
    baseline = await activities._result(run_id, "panel_baseline")
    assert baseline["questions"][0] == {
        "scored": 3,
        "mentioned": 1,
        "owned_domain_cited": 0,
        "shortlisted": 1,
        "selected_first": 0,
    }
    # This run: the target is named in two answers to the first question.
    activities.responses = SimpleNamespace(
        create=AsyncMock(
            side_effect=[
                answer("Acme is a good fit."),
                judge(True),
                answer("Acme is a good fit."),
                judge(True),
                *[answer() for _ in range(14)],
            ]
        )
    )
    # Twelve answers with web search, then one without for each of the four questions.
    for index in range(16):
        await activities.organic_observe({"run_id": run_id, "index": index})
    await activities.organic_start_crawl(run_id)
    assert await activities.organic_poll_crawl(run_id)
    await activities.organic_brand_checks(run_id)
    await activities.organic_publish(run_id)
    artifacts = await activities._result(run_id, "artifacts")
    report = artifacts[audit_paths(run_id)["AUDIT.md"]]
    assert "Three fresh answers per question." in report
    assert (
        "Question set: the same 4 questions as the audit started 2026-09-01 "
        f"(`{source.id}`), 3 answers each, so results compare." in report
    )
    assert "| Q1 | 1/3 → 2/3 | 0/3 → 0/3 | 1/3 → 2/3 | 0/3 → 0/3 |" in report
    assert "| All questions | 1/12 → 2/12 | 0/12 → 0/12 | 1/12 → 2/12 | 0/12 → 0/12 |" in report
    evidence = json.loads(artifacts[audit_paths(run_id)["evidence.json"]])
    comparison = evidence["ai_visibility"]["comparison"]
    assert comparison["baseline"]["source_run_id"] == str(source.id)
    assert comparison["current"][0]["mentioned"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"market": "GB"},
        {"host": "www.example.com"},
        {"repetitions": None},  # A set drafted before v10 carries no answer count.
    ],
)
async def test_questions_from_another_market_host_or_policy_are_not_reused(change):
    activities, db, _, _ = await activities_fixture()
    run_id = str(db.run.id)
    previous_audit(db, **change)
    assert await draft_first_panel(activities, run_id) == 32
    assert (await activities._result(run_id, "panel_preparation"))["method"] != (
        "reused_frozen_panel"
    )


@pytest.mark.asyncio
async def test_refresh_questions_drafts_a_new_set_and_starts_a_new_comparison():
    from dataclasses import replace

    activities, db, _, _ = await activities_fixture()
    db.run = replace(db.run, input={**db.run.input, "refresh_questions": True})
    run_id = str(db.run.id)
    previous_audit(db)
    assert await draft_first_panel(activities, run_id) == 32
    assert db.list_prerequisite_runs.await_count == 0
    assert await activities._result(run_id, "panel_baseline") is None


def test_content_findings_need_every_answer_to_a_question():
    from test_organic_audit_results import frozen_panel

    panel = limit_panel(frozen_panel(), max_jobs=2, repetitions=3)
    rows = [
        {
            "status": "completed",
            "index": index,
            "question_index": index // 3,
            "classification": {
                "mentioned": False,
                "owned_domain_cited": False,
                "shortlisted": False,
                "selected_first": False,
            },
        }
        for index in range(12)
    ]
    ai = summarize(panel, rows, policy_version=AUDIT_POLICY["version"])
    assert ai["summary"].startswith("12/12 planned observations completed.")
    assert "Three fresh answers per question." in ai["summary"]
    assert content_review_findings(ai, "example.com")[0]["affected_count"] == 4
    ai = summarize(panel, rows[:-1], policy_version=AUDIT_POLICY["version"])
    assert content_review_findings(ai, "example.com")[0]["affected_count"] == 3
    assert question_results(panel, rows[:-1])[3]["scored"] == 2
    # A set drafted before v10 keeps its two answers per question and its wording.
    old = summarize(frozen_panel(), rows[:8], policy_version=V9_AUDIT_POLICY["version"])
    assert "Two fresh answers per question." in old["summary"]


@pytest.mark.asyncio
async def test_a_retry_keeps_the_source_it_already_chose():
    activities, db, _, _ = await activities_fixture()
    run_id = str(db.run.id)
    first, panel = previous_audit(db, mentioned={0})
    await activities.organic_prepare_panel(run_id)
    # Lose the panel receipt as if the worker stopped after saving the baseline, while a
    # newer audit with its own question set was published meanwhile.
    del db.effects[activities.key(run_id, "panel")]
    newer, other = previous_audit(db)
    other = {**other, "questions": [dict(q) for q in other["questions"]]}
    other["questions"][0]["question"] = "Which apps help a small team schedule shared work?"
    other = {
        "status": "completed",
        **limit_panel(
            {k: v for k, v in other.items() if k != "status"},
            max_jobs=2,
            repetitions=3,
            unsearched=True,
        ),
    }
    from tin_lite.domain import EffectReceipt

    key = f"organic:{newer.id}:panel"
    db.effects[key] = EffectReceipt(key, "organic.audit", "completed", other)
    assert other["sha256"] != panel["sha256"]
    db.list_prerequisite_runs.return_value = [("organic.audit", newer), ("organic.audit", first)]
    assert await activities.organic_prepare_panel(run_id) == 16
    assert await activities._result(run_id, "panel") == panel
    baseline = await activities._result(run_id, "panel_baseline")
    assert baseline["source_run_id"] == str(first.id)
