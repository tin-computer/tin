"""Scoring, partial reporting, and pinned-v1 regression tests; no paid providers."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_organic_audit import activities_fixture, panel_fixture, response

from tin_lite.organic_audit import (
    AUDIT_POLICY,
    LEGACY_AUDIT_POLICY,
    ai_report_details,
    audit_paths,
    build_documents,
    canonical_json,
    content_review_findings,
    digest,
)
from tin_lite.organic_audit_ai import (
    AI_CONTRACT,
    LEGACY_AI_CONTRACT,
    V9_AI_SCHEMAS,
    AuditValidationError,
    classify_absent_target,
    payload,
    read_response,
    summarize,
    validate_panel,
)


def frozen_panel():
    return validate_panel(
        read_response(response(json.dumps(panel_fixture())), search=True), "example.com"
    )


def negative_judgment():
    return {
        "mentioned": False,
        "mention_quote": "",
        "shortlisted": False,
        "shortlist_quote": "",
        "selected_first": False,
        "first_choice_quote": "",
    }


def graded_judgment():
    """The v10 judge also grades whether the answer evaluates the target."""
    return {**negative_judgment(), "evaluated": False, "evaluation_quote": ""}


def scored(index, *, cited=False):
    return {
        "status": "completed",
        "index": index,
        "question_index": index // 2,
        "repetition": index % 2 + 1,
        "classification": {**negative_judgment(), "owned_domain_cited": cited},
    }


@pytest.mark.parametrize(
    "answer",
    [
        "For a customer-support or commerce agent, the official Messages for Business route "
        "is generally easier to defend operationally.",
        "Twilio recommends one subaccount per customer, with that customer’s phone numbers "
        "and Messaging Service(s) inside it.",
    ],
)
def test_pilot_false_positive_grades_are_unnecessary_when_target_is_absent(answer):
    panel = {**panel_fixture(), "name": "Claw Messenger", "aliases": ["Claw Messenger"]}
    result = classify_absent_target({"text": answer, "citations": []}, panel)
    assert result is not None and not any(
        result[k] for k in ("mentioned", "owned_domain_cited", "shortlisted", "selected_first")
    )


def test_absence_never_implies_no_citation_and_presence_still_needs_semantic_grading():
    panel = frozen_panel()
    result = classify_absent_target(
        {"text": "A useful background source.", "citations": ["https://example.com/facts"]}, panel
    )
    assert result["owned_domain_cited"] and not result["mentioned"]
    for text in ("ACME is not suitable.", "Acme Tools is mentioned as a namesake."):
        assert classify_absent_target({"text": text, "citations": []}, panel) is None
    with pytest.raises(AuditValidationError, match="empty"):
        classify_absent_target({"text": "", "citations": []}, panel)


async def observing_fixture(responses):
    activities, db, storage, provider = await activities_fixture()
    run_id = str(db.run.id)
    await activities._save(run_id, "panel", {"status": "completed", **frozen_panel()})
    activities.responses = SimpleNamespace(create=AsyncMock(side_effect=responses))
    return activities, db, storage, provider


@pytest.mark.asyncio
async def test_absence_skips_paid_grader_and_duplicate_execution_reuses_observation():
    activities, db, _, _ = await observing_fixture([response("Consider Rival Brand.")])
    run_id = str(db.run.id)
    for _ in range(2):
        await activities.organic_observe({"run_id": run_id, "index": 0})
    observation = await activities._result(run_id, "observation:0")
    assert observation["status"] == "completed"
    assert observation["scoring_method"] == "target_name_absent"
    assert activities.responses.create.await_count == 1
    assert await db.get_effect(activities.key(run_id, "judge:0")) is None
    budget = await db.get_effect(activities.key(run_id, "budget"))
    assert budget.result == {"answer:0": "0.20"}


@pytest.mark.asyncio
async def test_negative_target_mention_is_graded_not_treated_as_absent_or_recommended():
    text = "Acme is not suitable for this buyer."
    grade = {**negative_judgment(), "mentioned": True, "mention_quote": text}
    activities, db, _, _ = await observing_fixture(
        [
            response(text),
            response(json.dumps(grade), search=False),
        ]
    )
    run_id = str(db.run.id)
    await activities.organic_observe({"run_id": run_id, "index": 0})
    result = await activities._result(run_id, "observation:0")
    assert result["scoring_method"] == "evidence_checked_judge"
    assert result["classification"]["mentioned"] and not result["classification"]["shortlisted"]
    assert activities.responses.create.await_count == 2
    assert (
        activities.responses.create.await_args_list[1].args[0]["instructions"]
        == AI_CONTRACT["judge"]
    )


@pytest.mark.asyncio
async def test_unsupported_positive_stays_unknown_with_safe_specific_reason_and_no_retry():
    grade = {**negative_judgment(), "mentioned": True, "mention_quote": "Rival Brand is good."}
    activities, db, _, _ = await observing_fixture(
        [
            response("Acme is one option. Rival Brand is good."),
            response(json.dumps(grade), search=False),
        ]
    )
    run_id = str(db.run.id)
    for _ in range(2):
        await activities.organic_observe({"run_id": run_id, "index": 0})
    result = await activities._result(run_id, "observation:0")
    assert result["status"] == "unavailable" and "classification" not in result
    assert result["reason"] == "mention_quote_invalid" and result["failure_stage"] == "grading"
    assert activities.responses.create.await_count == 2


@pytest.mark.parametrize(
    "raw, expected_status, reason",
    [
        (RuntimeError("secret-bearing provider details"), "unknown", "provider_result_unavailable"),
        ({**response(), "status": "incomplete"}, "unavailable", "response_incomplete"),
        (response(""), "unavailable", "response_empty"),
        (response("x" * 33_000), "unavailable", "response_too_large"),
        (
            {"status": "completed", "id": "response", "output": [None]},
            "unavailable",
            "response_invalid",
        ),
        (response("An answer", search=False), "unavailable", "search_not_called"),
    ],
)
@pytest.mark.asyncio
async def test_response_failure_is_not_a_negative_or_resent_and_diagnostics_are_safe(
    raw, expected_status, reason
):
    activities, db, _, _ = await observing_fixture([raw])
    run_id = str(db.run.id)
    for _ in range(2):
        await activities.organic_observe({"run_id": run_id, "index": 0})
    result = await activities._result(run_id, "observation:0")
    assert result["status"] == "unavailable" and "classification" not in result
    assert result["reason"] == reason and result["failure_stage"] == "answer"
    assert result["answer"]["status"] == expected_status
    assert "secret-bearing" not in json.dumps(result)
    assert activities.responses.create.await_count == 1


def test_partial_counts_keep_unknowns_and_content_findings_require_complete_question_pairs():
    panel = frozen_panel()
    rows = [
        scored(0),
        scored(1),
        scored(2),
        {"index": 3, "status": "unavailable"},
        scored(4, cited=True),
        scored(5),
        {"index": 6, "status": "unavailable"},
        {"index": 7, "status": "unavailable"},
    ]
    summary = summarize(panel, rows)
    assert summary["status"] == "partial" and summary["metrics"] is None
    assert summary["completed"] == 5 and summary["missing"] == 3
    assert summary["observed_metrics"]["owned_domain_cited"] == 1
    assert "unknown, not negatives" in summary["summary"]
    findings = content_review_findings(summary, "example.com")
    assert len(findings) == 1 and findings[0]["affected_count"] == 1
    assert findings[0]["audit_coverage"] == "partial"
    assert findings[0]["verification"]["questions"][0]["observation_indexes"] == [0, 1]
    legacy = summarize(panel, rows, policy_version=LEGACY_AUDIT_POLICY["version"])
    assert "observed_metrics" not in legacy and legacy["metrics"] is None
    assert (
        content_review_findings(
            legacy, "example.com", policy_version=LEGACY_AUDIT_POLICY["version"]
        )
        == []
    )


def test_report_explains_each_gap_without_interpreting_provider_text_as_a_reason():
    panel = frozen_panel()
    rows = [
        scored(0),
        scored(1),
        scored(2),
        {
            "index": 3,
            "status": "unavailable",
            "failure_stage": "grading",
            "reason": "mention_quote_invalid",
        },
        {"index": 4, "status": "unavailable", "reason": "secret-bearing provider body"},
    ]
    ai = summarize(panel, rows)
    details = "\n".join(ai_report_details(ai))
    assert "| Q2." in details and "| 1/2 |" in details
    assert "Q2, answer 2 — Grading:" in details and "exact quote naming the target" in details
    assert "Q3, answer 1" in details and "Unknown" in details
    assert "secret-bearing" not in details
    assert "observed counts, not full-panel rates" in ai["summary"]


def test_all_unavailable_produces_no_findings_and_no_zero_visibility_score():
    panel = frozen_panel()
    ai = summarize(panel, [])
    assert ai["metrics"] is ai["observed_metrics"] is None and ai["missing"] == 8
    assert content_review_findings(ai, "example.com") == []
    assert "Unknown" in "\n".join(ai_report_details(ai))


@pytest.mark.asyncio
async def test_in_flight_v1_scope_and_grading_are_not_reinterpreted():
    grade = {**negative_judgment(), "mentioned": True, "mention_quote": "Rival Brand is good."}
    activities, db, _, _ = await observing_fixture(
        [
            response("Rival Brand is good."),
            response(json.dumps(grade), search=False),
        ]
    )
    run_id = str(db.run.id)
    key = activities.key(run_id, "scope")
    old_scope = dict(db.effects[key].result)
    old_scope.pop("policy_version")
    db.effects[key] = replace(db.effects[key], result=old_scope)
    await activities.organic_observe({"run_id": run_id, "index": 0})
    assert activities.responses.create.await_count == 2
    assert (
        activities.responses.create.await_args_list[1].args[0]["instructions"]
        == LEGACY_AI_CONTRACT["judge"]
    )
    result = await activities._result(run_id, "observation:0")
    assert result["reason"] == "invalid_answer_judgment" and result["status"] == "unavailable"


@pytest.mark.asyncio
async def test_worker_accepts_exact_legacy_definition_but_rejects_mixed_contract():
    activities, db, storage, _ = await activities_fixture()
    run_id = str(db.run.id)
    del db.effects[activities.key(run_id, "scope")]
    definition = json.loads(storage.read_canonical_artifact.return_value)
    definition.update(
        audit_policy=LEGACY_AUDIT_POLICY,
        audit_instructions=LEGACY_AI_CONTRACT,
        audit_schemas=V9_AI_SCHEMAS,
    )
    storage.read_canonical_artifact.return_value = canonical_json(definition)
    await activities.organic_prepare(run_id)
    assert await activities._policy_version(run_id) == LEGACY_AUDIT_POLICY["version"]
    del db.effects[activities.key(run_id, "scope")]
    definition["audit_instructions"] = AI_CONTRACT
    storage.read_canonical_artifact.return_value = canonical_json(definition)
    with pytest.raises(Exception, match="pinned audit version"):
        await activities.organic_prepare(run_id)


def test_new_report_pins_policy_and_preserves_digest_handoff():
    from uuid import uuid4

    run_id = str(uuid4())
    ai = summarize(frozen_panel(), [scored(0), scored(1)])
    args = {
        "run_id": run_id,
        "project_id": str(uuid4()),
        "definition_sha": "d" * 40,
        "scope": {
            "host": "example.com",
            "url": "https://example.com/",
            "market": "US",
            "started_at": "2026-09-08",
        },
        "crawl": {"status": "completed", "pages": []},
        "ai": ai,
        "spending": {},
    }
    docs = build_documents(**args)
    paths = audit_paths(run_id)
    evidence = json.loads(docs[paths["evidence.json"]])
    findings = json.loads(docs[paths["findings.json"]])
    assert evidence["policy"] == AUDIT_POLICY
    assert findings["evidence_sha256"] == digest(evidence)
    assert digest(findings).encode() in docs[paths["AUDIT.md"]]
    assert b"Buyer-question results" in docs[paths["AUDIT.md"]]
    legacy_ai = summarize(
        frozen_panel(), [scored(0), scored(1)], policy_version=LEGACY_AUDIT_POLICY["version"]
    )
    legacy = build_documents(
        **{**args, "ai": legacy_ai}, policy_version=LEGACY_AUDIT_POLICY["version"]
    )
    assert b"Buyer-question results" not in legacy[paths["AUDIT.md"]]
    assert json.loads(legacy[paths["evidence.json"]])["policy"] == LEGACY_AUDIT_POLICY


def test_unknown_policy_cannot_dispatch_with_current_instructions():
    with pytest.raises(ValueError, match="Unsupported"):
        payload(stage="judge", data={}, market="US", search=False, policy_version="future")
