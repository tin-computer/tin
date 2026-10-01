"""The trusted AI answers activity: receipts, retries and identifier-only results."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from test_ai_answers import Clock, FakeDataForSEO, client_for, inputs

from tin_lite.ai_answers_activities import (
    AIAnswersActivities,
    read_result,
    receipt_key,
    save_request,
)
from tin_lite.domain import EffectReceipt


class FakeRunDatabase:
    def __init__(self, status="running") -> None:
        self.run = SimpleNamespace(id=uuid4(), status=SimpleNamespace(value=status))
        self.receipts: dict[str, EffectReceipt] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.billing = None

    @asynccontextmanager
    async def effect_lock(self, key, operation, *, conn=None):
        async with self._locks.setdefault(key, asyncio.Lock()):
            yield None, self.receipts.get(key)

    async def get_effect(self, key, conn=None):
        return self.receipts.get(key)

    async def start_effect(self, conn, *, execution_key, operation):
        self.receipts.setdefault(
            execution_key, EffectReceipt(execution_key, operation, "started", None)
        )

    async def save_effect_progress(self, conn, *, execution_key, result):
        self.receipts[execution_key] = replace(self.receipts[execution_key], result=result)

    async def complete_effect(self, conn, *, execution_key, result):
        self.receipts[execution_key] = replace(
            self.receipts[execution_key], status="completed", result=result
        )

    async def get_run(self, run_id, conn=None):
        return self.run if UUID(str(run_id)) == self.run.id else None


def activities(db, fake):
    clock = Clock()
    return AIAnswersActivities(
        database=db,
        settings=SimpleNamespace(),
        client=client_for(fake),
        clock=clock,
        sleep=clock.sleep,
    )


async def test_activity_measures_the_saved_panel_and_returns_counts_only():
    db, fake = FakeRunDatabase(), FakeDataForSEO()
    control = {"run_id": str(db.run.id), "stage": "ai_answers"}
    await save_request(db, run_id=db.run.id, stage="ai_answers", inputs=inputs())
    # Saving the same panel twice is fine; a different one for the stage is refused.
    await save_request(db, run_id=db.run.id, stage="ai_answers", inputs=inputs())
    with pytest.raises(ValueError, match="different"):
        await save_request(
            db, run_id=db.run.id, stage="ai_answers", inputs=inputs(prompts=["other"])
        )
    work = activities(db, fake)
    summary = await ActivityEnvironment().run(work.ai_answers_measure, control)
    assert summary["status"] == "measured" and summary["rows"] == 12
    assert "form builder" not in json.dumps(summary) and "Acme" not in json.dumps(summary)
    saved = await read_result(db, run_id=db.run.id, stage="ai_answers")
    assert len(saved["rows"]) == 12 and saved["rows"][0]["answer"]
    # Paid requests are receipted by name; the saved summary answers a retry without calls.
    assert receipt_key(db.run.id, "ai_answers", "post:chatgpt") in db.receipts
    assert receipt_key(db.run.id, "ai_answers", "live:claude:1") in db.receipts
    calls = len(fake.requests)
    assert await ActivityEnvironment().run(work.ai_answers_measure, control) == summary
    assert len(fake.requests) == calls


async def test_activity_retry_reuses_receipts_and_never_resends_an_unconfirmed_call():
    db, fake = FakeRunDatabase(), FakeDataForSEO()
    stage = "ai_answers"
    await save_request(db, run_id=db.run.id, stage=stage, inputs=inputs(engines=["claude"]))
    key = receipt_key(db.run.id, stage, "live:claude:0")
    # A worker died after saving the intent but before the outcome.
    db.receipts[key] = EffectReceipt(key, "ai.answers", "started", {"attempted_at": "then"})
    summary = await ActivityEnvironment().run(
        activities(db, fake).ai_answers_measure, {"run_id": str(db.run.id), "stage": stage}
    )
    assert summary["unknown"] == 1 and summary["answered"] == 1
    assert [r[2][0]["tag"] for r in fake.posts()] == [f"tin-ai:{db.run.id}:{stage}:claude:1"]


async def test_activity_rejects_an_over_ceiling_panel_without_spending():
    db, fake = FakeRunDatabase(), FakeDataForSEO()
    await save_request(db, run_id=db.run.id, stage="panel", inputs=inputs(max_cost_usd="0.01"))
    summary = await ActivityEnvironment().run(
        activities(db, fake).ai_answers_measure, {"run_id": str(db.run.id), "stage": "panel"}
    )
    assert summary == {
        "status": "rejected",
        "reason": "cost_ceiling",
        "estimate_usd": "0.1552",
        "max_cost_usd": "0.01",
        "cost_usd": "0",
    }
    assert fake.requests == []


async def test_activity_refuses_without_a_panel_an_active_run_or_credentials():
    db = FakeRunDatabase()
    control = {"run_id": str(db.run.id), "stage": "ai_answers"}
    env = ActivityEnvironment()
    with pytest.raises(ApplicationError, match="panel"):
        await env.run(activities(db, FakeDataForSEO()).ai_answers_measure, control)
    await save_request(db, run_id=db.run.id, stage="ai_answers", inputs=inputs())
    unconfigured = AIAnswersActivities(database=db, settings=SimpleNamespace())
    with pytest.raises(ApplicationError, match="not configured"):
        await env.run(unconfigured.ai_answers_measure, control)
    db.run.status.value = "stopped"
    with pytest.raises(ApplicationError, match="no longer active"):
        await env.run(activities(db, FakeDataForSEO()).ai_answers_measure, control)
    with pytest.raises(ValueError):
        receipt_key(db.run.id, "Bad Stage!", "result")
