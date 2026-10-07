"""organic.technical_fix refuses new work; pinned paths keep their technical fix."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from tin_lite import technical_fix
from tin_lite.run_service import WorkflowInputError, start_workflow_run


def _workflow():
    return SimpleNamespace(
        id=uuid4(), key=technical_fix.KEY, executor="codex.procedure", project_id=None
    )


async def test_a_new_technical_fix_is_refused_with_the_website_change_route():
    runtime = SimpleNamespace(database=SimpleNamespace(get_run_by_start_key=AsyncMock()))
    with pytest.raises(WorkflowInputError, match="preflight_website_change"):
        await start_workflow_run(
            runtime=runtime,  # type: ignore[arg-type]
            settings=SimpleNamespace(),  # type: ignore[arg-type]
            workflow=_workflow(),  # type: ignore[arg-type]
            project_id=uuid4(),
            started_by_clerk_user_id="user_member",
            input_payload={},
        )
    runtime.database.get_run_by_start_key.assert_not_awaited()


@pytest.mark.parametrize(
    "pinned",
    [
        {"retry_of_run_id": uuid4()},
        {"project_workflow_id": uuid4()},
        {"_organic_parent_run_id": uuid4()},
        {"_system_step": True},
    ],
)
async def test_retries_schedules_and_older_traffic_systems_are_not_refused(monkeypatch, pinned):
    reached = AsyncMock(side_effect=RuntimeError("admission continued"))
    monkeypatch.setattr("tin_lite.run_service.resolve_execution_contract", reached)
    with pytest.raises(RuntimeError, match="admission continued"):
        await start_workflow_run(
            runtime=SimpleNamespace(database=SimpleNamespace(), storage=None),  # type: ignore[arg-type]
            settings=SimpleNamespace(),  # type: ignore[arg-type]
            workflow=_workflow(),  # type: ignore[arg-type]
            project_id=uuid4(),
            started_by_clerk_user_id="user_member",
            input_payload={},
            **pinned,
        )
    reached.assert_awaited_once()
