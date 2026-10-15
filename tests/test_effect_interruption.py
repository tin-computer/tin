"""A deploy that cancels a model call must leave a failed receipt and a terminal retry."""

import asyncio

import pytest
from temporalio.exceptions import ApplicationError
from test_procedure_publication import activity_fixture
from test_procedure_publication import publication_db as publication_db

from tin_lite.activities import INTERRUPTED_MODEL_REQUEST
from tin_lite.usage_capture import (
    ObservationAlreadyRecorded,
    begin_observation,
    external_usage_scope,
)

EFFECTS = [
    ("_weekly_brief_effect", "weekly_brief:model"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "suffix"), EFFECTS)
async def test_cancelled_model_call_fails_receipt_and_is_never_repeated(
    publication_db, method, suffix
):
    db = publication_db
    activities, _, run, _ = await activity_fixture(db)
    effect = getattr(activities, method)
    dispatched = asyncio.Event()

    async def interrupted_call():
        await begin_observation("openai", "model", "responses")
        dispatched.set()
        await asyncio.Event().wait()  # the deploy cancels the activity here

    task = asyncio.create_task(effect(run_id=run.id, execute=interrupted_call))
    await asyncio.wait_for(dispatched.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    receipt = await db.get_effect(f"{run.id}:{suffix}")
    assert receipt is not None and receipt.status == "failed"
    assert "CancelledError" in (receipt.error_message or "")

    async def must_not_run():
        raise AssertionError("an interrupted paid request was repeated")

    with pytest.raises(ApplicationError) as retried:
        await effect(run_id=run.id, execute=must_not_run)
    assert retried.value.non_retryable
    assert retried.value.type == "ModelRequestInterrupted"
    assert INTERRUPTED_MODEL_REQUEST in retried.value.message
    assert "already observed" not in retried.value.message
    receipt = await db.get_effect(f"{run.id}:{suffix}")
    assert receipt.status == "failed"
    assert INTERRUPTED_MODEL_REQUEST in (receipt.error_message or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "suffix"), EFFECTS)
async def test_observation_conflict_inside_the_call_is_terminal(publication_db, method, suffix):
    db = publication_db
    activities, _, run, _ = await activity_fixture(db)
    effect = getattr(activities, method)

    async def second_request():
        await begin_observation("openai", "model", "other-endpoint")
        await begin_observation("openai", "model", "other-endpoint")

    with pytest.raises(ApplicationError) as failed:
        await effect(run_id=run.id, execute=second_request)
    assert failed.value.non_retryable
    assert isinstance(failed.value.__cause__, ObservationAlreadyRecorded)
    assert isinstance(failed.value.__cause__, RuntimeError)  # older callers still match
    receipt = await db.get_effect(f"{run.id}:{suffix}")
    assert receipt.status == "failed"


@pytest.mark.asyncio
async def test_receipt_wedged_as_started_by_an_older_release_is_recovered(publication_db):
    db = publication_db
    activities, _, run, _ = await activity_fixture(db)
    key = f"{run.id}:weekly_brief:model"
    async with db.effect_lock(key, "weekly_brief_model") as (conn, _):
        await db.start_effect(conn, execution_key=key, operation="weekly_brief_model")
        with external_usage_scope(db, conn, run.id, "weekly_brief"):
            await begin_observation("openai", "model", "responses")

    async def must_not_run():
        raise AssertionError("an interrupted paid request was repeated")

    with pytest.raises(ApplicationError, match="was not repeated") as retried:
        await activities._weekly_brief_effect(run_id=run.id, execute=must_not_run)
    assert retried.value.non_retryable
    assert (await db.get_effect(key)).status == "failed"
