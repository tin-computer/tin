"""Grounded panel preparation and provider-contract regressions; no paid calls."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_organic_audit import activities_fixture, panel_fixture, response

from tin_lite.organic_audit import (
    AUDIT_POLICY,
    V2_AUDIT_POLICY,
    V3_AUDIT_POLICY,
    V5_AUDIT_POLICY,
    V6_AUDIT_POLICY,
    ai_report_details,
    canonical_json,
)
from tin_lite.organic_audit_ai import (
    V2_AI_CONTRACT,
    V3_AI_CONTRACT,
    V4_AI_CONTRACT,
    V6_AI_CONTRACT,
    V9_AI_SCHEMAS,
    AuditValidationError,
    ai_contract,
    read_response,
)
from tin_lite.organic_audit_panel import grounded_panel, interpret_questions


def read(raw, *, search=True):
    return read_response(raw, search=search, policy_version=AUDIT_POLICY["version"])


def research():
    return response("Acme provides team planning software. Buyers coordinate weekly work.")


def draft(panel=None):
    return response(json.dumps(panel or panel_fixture()), search=False)


def review(accepted=True, *, rejected=(), legacy=False):
    """A panel review: v11's per-question shape, or the whole-panel verdict older pins use."""
    value = {"accepted": accepted, "explanation": "The public research supports these buyer jobs."}
    if not legacy:
        value["rejected_questions"] = [
            {"number": number, "reason": "It could describe a different buying category."}
            for number in rejected
        ]
    return response(json.dumps(value), search=False)


def interpretation(text="1–4: The buyer seeks software to coordinate team work."):
    return response(text, search=False)


def interpretations():
    return [interpretation() for _ in range(4)]


def test_source_urls_include_completed_page_actions_but_not_failed_attempts():
    raw = research()
    raw["output"][:1] = [
        {
            "type": "web_search_call",
            "status": "completed",
            "action": {"type": "open_page", "url": "https://example.com/", "sources": None},
        },
        {
            "type": "web_search_call",
            "status": "completed",
            "action": {"type": "find_in_page", "url": "https://example.com/product"},
        },
        {
            "type": "web_search_call",
            "status": "failed",
            "action": {"type": "open_page", "url": "https://not-observed.test/"},
        },
    ]
    result = read(raw)
    assert result["sources"] == ["https://example.com/", "https://example.com/product"]
    assert result["diagnostics"]["search_statuses"] == ["completed", "completed", "failed"]
    proposed = panel_fixture()
    proposed["questions"][0]["source_url"] = "https://example.com/product"
    assert grounded_panel(read(draft(proposed), search=False), result, "example.com")


def test_processed_search_cap_is_not_confused_with_failed_extra_attempt():
    raw = research()
    raw["output"][:1] = [deepcopy(raw["output"][0]) for _ in range(3)] + [
        {"type": "web_search_call", "status": "failed", "action": {}}
    ]
    assert read(raw)["diagnostics"]["search_call_count"] == 4
    with pytest.raises(AuditValidationError, match="bounded web search"):
        read_response(raw, search=True, policy_version=V2_AUDIT_POLICY["version"])
    raw["output"][3]["status"] = "completed"
    with pytest.raises(AuditValidationError) as exc:
        read(raw)
    assert exc.value.reason == "search_limit_exceeded"


@pytest.mark.parametrize("status", ["searching", "in_progress"])
def test_v4_completed_response_can_retain_ignored_attempt_after_processed_cap(status):
    raw = research()
    raw["output"][:1] = [deepcopy(raw["output"][0]) for _ in range(3)] + [
        {
            "type": "web_search_call",
            "status": status,
            "action": {"type": "open_page", "url": "https://unobserved.test/"},
        }
    ]
    result = read(raw)
    assert result["sources"] == ["https://example.com/"]
    assert result["diagnostics"]["search_statuses"][-1] == status
    with pytest.raises(AuditValidationError):
        read_response(raw, search=True, policy_version=V3_AUDIT_POLICY["version"])
    raw["status"] = "incomplete"
    with pytest.raises(AuditValidationError):
        read(raw)
    raw["status"] = "completed"
    raw["output"].pop(0)
    with pytest.raises(AuditValidationError):
        read(raw)


@pytest.mark.parametrize("status", ["failed", "in_progress", "searching", None])
def test_search_without_any_completed_call_is_never_scored(status):
    raw = research()
    raw["output"][0]["status"] = status
    with pytest.raises(AuditValidationError) as exc:
        read(raw)
    assert exc.value.reason == "search_not_completed"


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda p: p.update(host="parent.example.com"), "panel_identity_invalid"),
        (
            lambda p: p["questions"][0].update(source_url="https://example.com/invented"),
            "panel_source_unobserved",
        ),
        (
            lambda p: p["questions"][0].update(
                question="Why should I use Acme for planning team work?"
            ),
            "panel_questions_invalid",
        ),
        (
            lambda p: p["questions"][0].update(question="Pick exactly one tool for team planning."),
            "panel_questions_invalid",
        ),
    ],
)
def test_deterministic_rules_keep_target_and_source_boundaries(mutation, reason):
    proposed = panel_fixture()
    mutation(proposed)
    with pytest.raises(AuditValidationError) as exc:
        grounded_panel(read(draft(proposed), search=False), read(research()), "example.com")
    assert exc.value.reason == reason


@pytest.mark.asyncio
async def test_research_then_no_tool_draft_then_validation_freezes_once():
    activities, db, _, _ = await activities_fixture()
    activities.responses = SimpleNamespace(
        create=AsyncMock(side_effect=[research(), draft(), *interpretations(), review()])
    )
    run_id = str(db.run.id)
    for _ in range(2):
        # v10 asks every frozen question three times with web search and once without:
        # 4 questions, 16 observations.
        assert await activities.organic_prepare_panel(run_id) == 16
    requests = [call.args[0] for call in activities.responses.create.await_args_list]
    assert len(requests) == 7
    assert "text" not in requests[0]
    assert requests[0]["tools"][0]["filters"] == {"allowed_domains": ["example.com"]}
    assert requests[1]["tools"] == [] and requests[2]["tools"] == []
    assert requests[3]["tools"] == []
    for index, request in enumerate(requests[2:6]):
        assert request["input"] == panel_fixture()["questions"][index]["question"]
        assert "Acme" not in request["input"]
        assert "example.com" not in request["input"]
    assert json.loads(requests[6]["input"])["blind_interpretation"]
    assert json.loads(requests[1]["input"])["observed_source_urls"] == ["https://example.com/"]
    preparation = await activities._result(run_id, "panel_preparation")
    assert preparation["status"] == "completed" and len(preparation["research_sha256"]) == 64
    assert await activities._result(run_id, "answer:0") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["citation", "semantic"])
async def test_bad_panel_gets_one_grounded_correction_without_research_repeat(failure):
    bad = panel_fixture()
    bad["questions"][0]["source_url"] = "https://example.com/invented"
    calls = (
        [research(), draft(bad)]
        if failure == "citation"
        else [research(), draft(), *interpretations(), review(False)]
    )
    activities, db, _, _ = await activities_fixture()
    activities.responses = SimpleNamespace(
        create=AsyncMock(side_effect=[*calls, draft(), *interpretations(), review()])
    )
    run_id = str(db.run.id)
    assert await activities.organic_prepare_panel(run_id) == 16
    assert await activities.organic_prepare_panel(run_id) == 16
    assert activities.responses.create.await_count == len(calls) + 6
    requests = [call.args[0] for call in activities.responses.create.await_args_list]
    assert sum(bool(req["tools"]) for req in requests) == 1
    correction = json.loads(requests[-6]["input"])["correction"]
    expected = "panel_source_unobserved" if failure == "citation" else "panel_review_rejected"
    assert correction["reason"] == expected
    assert len((await activities._result(run_id, "panel_preparation"))["attempts"]) == 3


@pytest.mark.asyncio
async def test_repeated_invalid_panel_stops_and_report_names_real_failure():
    bad = panel_fixture()
    bad["questions"][0]["source_url"] = "https://example.com/invented"
    activities, db, _, _ = await activities_fixture()
    activities.responses = SimpleNamespace(
        create=AsyncMock(side_effect=[research(), draft(bad), draft(bad)])
    )
    run_id = str(db.run.id)
    assert await activities.organic_prepare_panel(run_id) == 0
    assert await activities.organic_prepare_panel(run_id) == 0
    preparation = await activities._result(run_id, "panel_preparation")
    assert preparation["reason"] == "panel_source_unobserved"
    assert activities.responses.create.await_count == 3
    report = "\n".join(ai_report_details({"preparation": preparation}))
    assert "absent from the saved public research" in report


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
async def test_only_known_research_failure_can_use_one_recovery(unknown):
    failed = TimeoutError() if unknown else response("Unsearched facts", search=False)
    activities, db, _, _ = await activities_fixture()
    activities.responses = SimpleNamespace(
        create=AsyncMock(side_effect=[failed, research(), draft(), *interpretations(), review()])
    )
    run_id = str(db.run.id)
    assert await activities.organic_prepare_panel(run_id) == (0 if unknown else 16)
    await activities.organic_prepare_panel(run_id)
    assert activities.responses.create.await_count == (1 if unknown else 8)
    receipt = await activities._result(run_id, "panel_research")
    if not unknown:
        assert receipt["diagnostics"]["search_call_count"] == 0
        assert receipt["reason"] == "search_not_called"


@pytest.mark.asyncio
async def test_recovery_respects_budget_and_never_becomes_unmetered():
    activities, db, _, _ = await activities_fixture(budget="0.20")
    activities.responses = SimpleNamespace(
        create=AsyncMock(return_value=response("No search", search=False))
    )
    run_id = str(db.run.id)
    assert await activities.organic_prepare_panel(run_id) == 0
    assert activities.responses.create.await_count == 1
    assert (await activities._result(run_id, "panel_preparation"))["reason"] == "spending_limit"


@pytest.mark.asyncio
async def test_exact_v2_definition_keeps_single_call_panel_and_old_payload():
    activities, db, storage, _ = await activities_fixture()
    run_id = str(db.run.id)
    del db.effects[activities.key(run_id, "scope")]
    definition = json.loads(storage.read_canonical_artifact.return_value)
    definition.update(
        audit_policy=V2_AUDIT_POLICY, audit_instructions=V2_AI_CONTRACT, audit_schemas=V9_AI_SCHEMAS
    )
    storage.read_canonical_artifact.return_value = canonical_json(definition)
    await activities.organic_prepare(run_id)
    activities.responses = SimpleNamespace(
        create=AsyncMock(side_effect=[response(json.dumps(panel_fixture())), review(legacy=True)])
    )
    assert await activities.organic_prepare_panel(run_id) == 8
    request = activities.responses.create.await_args_list[0].args[0]
    assert request["instructions"] == V2_AI_CONTRACT["panel"]
    assert await activities._result(run_id, "panel_preparation") is None


@pytest.mark.asyncio
async def test_exact_v3_definition_keeps_original_three_step_prompts():
    activities, db, storage, _ = await activities_fixture()
    run_id = str(db.run.id)
    del db.effects[activities.key(run_id, "scope")]
    definition = json.loads(storage.read_canonical_artifact.return_value)
    definition.update(
        audit_policy=V3_AUDIT_POLICY, audit_instructions=V3_AI_CONTRACT, audit_schemas=V9_AI_SCHEMAS
    )
    storage.read_canonical_artifact.return_value = canonical_json(definition)
    await activities.organic_prepare(run_id)
    activities.responses = SimpleNamespace(
        create=AsyncMock(side_effect=[research(), draft(), review(legacy=True)])
    )
    assert await activities.organic_prepare_panel(run_id) == 8
    requests = [call.args[0] for call in activities.responses.create.await_args_list]
    for request, stage in zip(requests, ("research", "panel", "validate"), strict=True):
        assert request["instructions"] == V3_AI_CONTRACT[stage]
    assert (await activities._result(run_id, "panel_preparation"))["status"] == "completed"


def test_v4_review_distinguishes_buying_intent_hypotheses_and_real_brand_names():
    contract = ai_contract(AUDIT_POLICY["version"])
    assert "BUYER asks an assistant" in contract["panel"]
    assert "Buyer questions are hypotheses" in contract["validate"]
    assert "descriptive taglines" in contract["validate"]
    assert "independent buyer markets" in contract["research"]
    assert ai_contract(V3_AUDIT_POLICY["version"]) == V3_AI_CONTRACT
    assert "BUYER asks an assistant" not in V3_AI_CONTRACT["panel"]


@pytest.mark.asyncio
async def test_standalone_review_cannot_use_job_labels_to_rescue_an_ambiguous_question():
    bad = panel_fixture()
    bad["questions"][3]["question"] = "What messaging APIs run from Docker or Linux?"
    bad["questions"][3]["fit_reason"] = "This really means phone communication for an AI agent."
    activities, db, _, _ = await activities_fixture()
    activities.responses = SimpleNamespace(
        create=AsyncMock(
            side_effect=[
                research(),
                draft(bad),
                *interpretations()[:3],
                interpretation("A software message queue."),
                review(False),
                draft(),
                *interpretations(),
                review(),
            ]
        )
    )
    assert await activities.organic_prepare_panel(str(db.run.id)) == 16
    calls = activities.responses.create.await_args_list
    for index in (6, 12):
        request = calls[index].args[0]
        data = json.loads(request["input"])
        assert "panel" not in data
        assert all(
            set(question) == {"number", "question"} for question in data["standalone_questions"]
        )
        assert "strongest reasonable alternative interpretation" in request["instructions"]
    notes = json.loads(json.loads(calls[6].args[0]["input"])["blind_interpretation"])
    assert notes[3] == {"number": 4, "interpretation": "A software message queue."}
    feedback = json.loads(calls[7].args[0]["input"])["correction"]["blind_interpretation"]
    assert json.loads(feedback) == notes
    corrected = await activities._result(str(db.run.id), "panel")
    assert corrected["questions"] == panel_fixture()["questions"]
    # No answer has yet been sampled; correcting preparation is not resampling a score.
    assert not any(":answer:" in key for key in db.effects)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy,contract", [(V5_AUDIT_POLICY, V4_AI_CONTRACT), (V6_AUDIT_POLICY, V6_AI_CONTRACT)]
)
async def test_exact_prior_definition_keeps_its_original_reviewer_and_evidence_bounds(
    policy, contract
):
    activities, db, storage, _ = await activities_fixture()
    run_id = str(db.run.id)
    del db.effects[activities.key(run_id, "scope")]
    definition = json.loads(storage.read_canonical_artifact.return_value)
    definition.update(audit_policy=policy, audit_instructions=contract, audit_schemas=V9_AI_SCHEMAS)
    storage.read_canonical_artifact.return_value = canonical_json(definition)
    await activities.organic_prepare(run_id)
    activities.responses = SimpleNamespace(
        create=AsyncMock(side_effect=[research(), draft(), review(legacy=True)])
    )
    assert await activities.organic_prepare_panel(run_id) == 8
    request = activities.responses.create.await_args_list[-1].args[0]
    assert request["instructions"] == contract["validate"]
    assert "blind_interpretation" not in json.loads(request["input"])
    assert activities.responses.create.await_count == 3
    assert (
        len(
            read_response(response("x" * 25_000), search=True, policy_version=policy["version"])[
                "text"
            ]
        )
        == 25_000
    )


@pytest.mark.asyncio
async def test_unknown_blind_interpretation_is_not_repeated_or_measured():
    activities, db, _, _ = await activities_fixture()
    activities.responses = SimpleNamespace(
        create=AsyncMock(side_effect=[research(), draft(), TimeoutError(), *interpretations()[:3]])
    )
    run_id = str(db.run.id)
    assert await activities.organic_prepare_panel(run_id) == 0
    assert await activities.organic_prepare_panel(run_id) == 0
    assert activities.responses.create.await_count == 6
    assert (await activities._result(run_id, "panel_interpretation:0"))["status"] == "unknown"
    assert not any(":answer:" in key for key in db.effects)


@pytest.mark.asyncio
async def test_blind_readers_are_bounded_and_keep_each_question_separate():
    active, peak = 0, 0
    requests = []

    async def model(run_id, stage, request, *, search):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        requests.append((stage, request["input"]))
        await asyncio.sleep(0)
        active -= 1
        return {"status": "completed", "value": {"text": request["input"]}}

    questions = [{"question": f"Independent question {index}"} for index in range(12)]
    result = await interpret_questions(
        SimpleNamespace(_model=model),
        "run",
        questions,
        {"policy_version": AUDIT_POLICY["version"], "market": "US"},
        "",
    )
    assert peak == 4
    assert len({stage for stage, _ in requests}) == 12
    assert {text for _, text in requests} == {q["question"] for q in questions}
    assert [row["number"] for row in json.loads(result["value"]["text"])] == list(range(1, 13))
