"""The rendered audit's limits match its recorded engine evidence; offline only."""

from __future__ import annotations

import pytest
from test_ai_answers import FakeDataForSEO
from test_organic_audit_engines import audit, measured_audit
from test_organic_audit_v12 import RUN_ID, documents, site_evidence

from tin_lite.organic_audit import (
    AUDIT_POLICY,
    V12_AUDIT_POLICY,
    V13_AUDIT_POLICY,
    V16_AUDIT_POLICY,
    audit_paths,
)
from tin_lite.organic_audit_engines import LABELS

SINGLE_ENGINE_LIMIT = "AI engines other than the one named above"
ENGINE_SCOPE = (
    "Missing or unconfirmed answers are not results. The sampled API baseline remains "
    "separate from those app and API-model measurements."
)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [None, "perplexity"])
async def test_engine_tables_and_closing_scope_agree(missing):
    fake = FakeDataForSEO()
    if missing:
        fake.live_error = {missing}
    run_id, _, files = await measured_audit(fake)
    report = files[audit_paths(run_id)["AUDIT.md"]].decode()

    assert "Tin asked 8 of the 8 buyer questions above on 6 AI engines" in report
    assert "In the apps people use (consumer_app_answer)" in report
    assert "From API models (api_model_answer)" in report
    assert SINGLE_ENGINE_LIMIT not in report
    assert ENGINE_SCOPE in report
    if missing:
        assert "The separate cross-engine tables record 40 answers from 5 engines." in report
        assert "| Perplexity API model (sonar) | 0/8 |" in report
        assert "the outcome could not be confirmed and was not bought again" in report
    else:
        assert "The separate cross-engine tables record 48 answers from 6 engines." in report
        assert "| Perplexity API model (perplexity-model-2026) | 8/8 |" in report


@pytest.mark.asyncio
@pytest.mark.parametrize("answered", [0, 1])
async def test_no_answer_rows_are_not_counted_as_measured_answers(answered):
    answers = {(engine, index): ("", []) for engine in LABELS for index in range(8)}
    if answered:
        answers[("chatgpt", 0)] = ("A recorded answer.", [])
    run_id, _, files = await measured_audit(FakeDataForSEO(answers=answers))
    report = files[audit_paths(run_id)["AUDIT.md"]].decode()

    expected = (
        "The separate cross-engine tables record 1 answer from 1 engine."
        if answered
        else "No cross-engine answers were measured for this run."
    )
    assert expected in report
    assert "| Perplexity API model (perplexity-model-2026) | 0/8 |" in report
    assert ENGINE_SCOPE in report and SINGLE_ENGINE_LIMIT not in report


@pytest.mark.asyncio
async def test_unfinished_measurement_does_not_claim_six_engine_coverage():
    activities, _, storage, run_id = await audit()
    await activities.organic_start_crawl(run_id)
    assert await activities.organic_poll_crawl(run_id)
    await activities.organic_end_crawl(run_id)
    assert await activities.organic_prepare_ai_engines(run_id) == "engines"
    await activities.organic_publish(run_id)
    report = storage.repo.trees[storage.repo.head][audit_paths(run_id)["AUDIT.md"]][1].decode()

    assert "Not measured. The measurement did not finish." in report
    assert "Tin asked" not in report and "| ChatGPT app" not in report
    assert SINGLE_ENGINE_LIMIT not in report
    assert ENGINE_SCOPE in report
    assert "No cross-engine answers were measured for this run." in report


@pytest.mark.asyncio
async def test_current_policy_with_no_engine_plan_keeps_not_measured_scope():
    files, pages = await site_evidence(AUDIT_POLICY)
    report = documents(AUDIT_POLICY, files, pages)[audit_paths(RUN_ID)["AUDIT.md"]].decode()

    assert "Not measured. The measurement was not started." in report
    assert "Tin asked" not in report and "| ChatGPT app" not in report
    assert SINGLE_ENGINE_LIMIT not in report
    assert ENGINE_SCOPE in report
    assert "No cross-engine answers were measured for this run." in report


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", [V12_AUDIT_POLICY, V13_AUDIT_POLICY, V16_AUDIT_POLICY])
async def test_pinned_policies_keep_their_original_limit(policy):
    files, pages = await site_evidence(policy)
    report = documents(policy, files, pages)[audit_paths(RUN_ID)["AUDIT.md"]].decode()

    assert SINGLE_ENGINE_LIMIT in report
    assert ENGINE_SCOPE not in report
