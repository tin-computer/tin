"""Retired for new work: organic.site_architecture, content.blog_index and website.change's
blog index source. Retries, saved schedules and older organic system runs keep what they pinned."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from tin_lite.run_service import WorkflowInputError, start_workflow_run


def _workflow(key, executor):
    return SimpleNamespace(id=uuid4(), key=key, executor=executor, project_id=None)


RETIRED = [
    ("organic.site_architecture", "workflow.code", {}, "page decisions"),
    ("content.blog_index", "codex.procedure", {}, "website.change adds each"),
    ("website.change", "codex.procedure", {"source": "blog_index"}, "source audit or planned"),
]


@pytest.mark.parametrize(("key", "executor", "inputs", "points_to"), RETIRED)
async def test_a_new_run_is_refused_with_where_the_work_goes(key, executor, inputs, points_to):
    runtime = SimpleNamespace(database=SimpleNamespace(get_run_by_start_key=AsyncMock()))
    with pytest.raises(WorkflowInputError, match=points_to):
        await start_workflow_run(
            runtime=runtime,  # type: ignore[arg-type]
            settings=SimpleNamespace(),  # type: ignore[arg-type]
            workflow=_workflow(key, executor),  # type: ignore[arg-type]
            project_id=uuid4(),
            started_by_clerk_user_id="user_member",
            input_payload=inputs,
        )


@pytest.mark.parametrize(("key", "executor", "inputs", "points_to"), RETIRED)
@pytest.mark.parametrize(
    "pinned",
    [
        {"retry_of_run_id": uuid4()},
        {"project_workflow_id": uuid4()},
        {"_organic_parent_run_id": uuid4()},
    ],
)
async def test_pinned_paths_are_not_refused(monkeypatch, key, executor, inputs, points_to, pinned):
    reached = AsyncMock(side_effect=RuntimeError("admission continued"))
    monkeypatch.setattr("tin_lite.run_service.resolve_execution_contract", reached)
    with pytest.raises(RuntimeError, match="admission continued"):
        await start_workflow_run(
            runtime=SimpleNamespace(database=SimpleNamespace(), storage=None),  # type: ignore[arg-type]
            settings=SimpleNamespace(),  # type: ignore[arg-type]
            workflow=_workflow(key, executor),  # type: ignore[arg-type]
            project_id=uuid4(),
            started_by_clerk_user_id="user_member",
            input_payload=inputs,
            **pinned,
        )


@pytest.mark.parametrize("source", ["audit", "planned", "content_draft"])
def test_other_website_change_sources_stay_open(source):
    from tin_lite.run_service import retired_for_new_work

    assert retired_for_new_work("website.change", {"source": source}) is None


def test_hidden_from_new_setups_but_still_registered():
    from tin_lite.growth_plan import PROGRAMS
    from tin_lite.public_workflows import PUBLIC_WORKFLOWS
    from tin_lite.workflow_order import WORKFLOW_DISPLAY_ORDER

    entries = {w.key: w for w in PUBLIC_WORKFLOWS}
    for key in ("organic.site_architecture", "content.blog_index"):
        assert entries[key].public_discovery is False
        assert key not in WORKFLOW_DISPLAY_ORDER
        assert key not in PROGRAMS["workflow_titles"]
        assert all(key not in p["tin"]["workflows"] for p in PROGRAMS["programs"])
