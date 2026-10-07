"""Waiting can cross workflow histories without restarting or failing the collection."""

import pytest
from temporalio import workflow

from tin_lite.workflows import ConnectionCollectionWorkflow


class Continued(workflow.ContinueAsNewError):
    pass


@pytest.mark.parametrize("new_history", [False, True])
async def test_waiting_continues_with_only_run_id_and_preserves_old_history(
    monkeypatch, new_history
):
    calls, timers = [], []
    polls = 0
    run_id = "00000000-0000-4000-8000-000000000001"

    async def execute(name, identifier, **_):
        nonlocal polls
        calls.append(name)
        assert identifier == run_id
        if name == "collection_poll":
            polls += 1
            return polls > 500

    async def sleep(duration):
        timers.append(duration.total_seconds())

    def continue_run(identifier):
        assert identifier == run_id
        raise Continued()

    monkeypatch.setattr(workflow, "execute_activity", execute)
    monkeypatch.setattr(workflow, "sleep", sleep)
    monkeypatch.setattr(workflow, "patched", lambda name: new_history)
    monkeypatch.setattr(workflow, "continue_as_new", continue_run)
    if new_history:
        with pytest.raises(Continued):
            await ConnectionCollectionWorkflow().run(run_id)
        assert polls == 500 and "collection_publish" not in calls
    else:
        await ConnectionCollectionWorkflow().run(run_id)
        assert polls == 501 and calls[-1] == "collection_publish"
    assert calls[0] == "collection_prepare"
    assert "collection_failure" not in calls
    assert timers == [15] * 500
