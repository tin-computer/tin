"""Explicit retries reuse completed keyword research without repeating supplier reads."""

import json
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from temporalio.exceptions import ApplicationError
from test_keyword_plan import finish, fixture

from tin_lite.domain import RunStatus
from tin_lite.keyword_plan import paths


async def retry_fixture(*, modern=False):
    activities, db, storage, provider, model = await fixture(modern=modern)
    original_id = str(db.run.id)
    await activities.keyword_collect(original_id)
    for index in range(await activities.keyword_sample_count(original_id)):
        await activities.keyword_inspect({"run_id": original_id, "index": index})
    original = replace(db.run, status=RunStatus.FAILED)
    db.run = replace(db.run, id=uuid4(), retry_of_run_id=original.id)

    async def get_run(run_id, **kwargs):
        return original if run_id == original.id else db.run if run_id == db.run.id else None

    db.get_run = get_run
    await activities.keyword_prepare(str(db.run.id))
    return activities, db, storage, provider, model, original


@pytest.mark.parametrize("modern", [False, True, "v3"])
async def test_retry_finishes_from_saved_research_with_only_a_new_review(modern):
    activities, db, storage, provider, model, original = await retry_fixture(modern=modern)
    previous_model_calls = model.generate.await_count
    paid_reads = provider.query.await_count
    await finish(activities, str(db.run.id))
    assert provider.query.await_count == paid_reads
    assert model.generate.await_count == previous_model_calls + 1
    assert storage.repo.writes == 1
    evidence = json.loads(
        storage.repo.trees[storage.repo.head][paths(str(db.run.id))["evidence.json"]][1]
    )
    assert evidence["reused_research_from_run_id"] == str(original.id)
    collection = await activities._result(str(db.run.id), "collection")
    assert collection["reused_from_run_id"] == str(original.id)
    sample = await activities._result(str(db.run.id), "serp:0")
    old_sample = await activities._result(str(original.id), "serp:0")
    assert sample["value"] == old_sample["value"]  # Original observations and dates survive.
    assert sample["reused_from_run_id"] == str(original.id)
    # A repeated activity recovers its saved result without another provider/model call.
    await finish(activities, str(db.run.id))
    assert provider.query.await_count == paid_reads
    assert model.generate.await_count == previous_model_calls + 1


@pytest.mark.parametrize("change", ["project", "workflow", "definition", "inputs", "no_retry"])
async def test_only_an_explicit_retry_of_the_same_job_reuses_research(change):
    activities, db, _, provider, _, _ = await retry_fixture()
    changes = {
        "project": {"project_id": uuid4()},
        "workflow": {"workflow_id": uuid4()},
        "definition": {"definition_commit_sha": "f" * 40},
        "inputs": {"input": {**db.run.input, "market": "GB"}},
        "no_retry": {"retry_of_run_id": None},
    }
    db.run = replace(db.run, **changes[change])
    assert not await activities._reuse_collected_research(db.run)
    assert await activities._result(str(db.run.id), "collection") is None


@pytest.mark.parametrize("unattempted", [False, True])
async def test_retry_preserves_unknown_reads_and_only_buys_unattempted_ones(unattempted):
    activities, db, _, provider, _, original = await retry_fixture()
    uncertain_key = activities.key(str(original.id), "serp:0")
    receipt = db.effects[uncertain_key]
    db.effects[uncertain_key] = replace(
        receipt,
        status="started",
        result={"request_sha256": receipt.result["request_sha256"], "attempted_at": "saved"},
    )
    if unattempted:
        del db.effects[uncertain_key]
    count = await activities.keyword_sample_count(str(original.id))
    paid_reads = provider.query.await_count
    await activities.keyword_collect(str(db.run.id))
    for index in range(count):
        await activities.keyword_inspect({"run_id": str(db.run.id), "index": index})
    assert provider.query.await_count == paid_reads + int(unattempted)
    assert (await activities._result(str(db.run.id), "serp:0"))["status"] == (
        "completed" if unattempted else "unknown"
    )


async def test_interrupted_reuse_resumes_before_marking_collection_complete(monkeypatch):
    activities, db, _, provider, _, _ = await retry_fixture()
    save = activities._save
    calls = 0

    async def interrupt(run_id, stage, value):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ConnectionError("lost database connection")
        return await save(run_id, stage, value)

    monkeypatch.setattr(activities, "_save", interrupt)
    with pytest.raises(ConnectionError):
        await activities.keyword_collect(str(db.run.id))
    assert await activities._result(str(db.run.id), "collection") is None
    paid_reads = provider.query.await_count
    monkeypatch.setattr(activities, "_save", save)
    await activities.keyword_collect(str(db.run.id))
    assert await activities._result(str(db.run.id), "collection")
    assert provider.query.await_count == paid_reads


async def test_mismatched_saved_request_stops_before_another_purchase():
    activities, db, _, provider, _, original = await retry_fixture()
    key = activities.key(str(original.id), "serp:0")
    db.effects[key] = replace(db.effects[key], result={"request_sha256": "wrong"})
    provider.query = AsyncMock()
    with pytest.raises(ApplicationError, match="does not match"):
        await activities.keyword_collect(str(db.run.id))
    provider.query.assert_not_awaited()
