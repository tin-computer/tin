from uuid import uuid4

import pytest

from tin_lite import intel_world
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.workflow_inputs import WorkflowInputError, normalize_workflow_inputs


def _schema() -> dict:
    (entry,) = [item for item in BUILTIN_WORKFLOWS if item.key == intel_world.KEY]
    return entry.input_schema


def test_saved_world_inputs_start_cleanly() -> None:
    # `tin-lite intel-world enable` saves these; a start re-normalizes them.
    assert (
        normalize_workflow_inputs(
            schema=_schema(), project_id=uuid4(), inputs=intel_world.saved_inputs()
        )
        == {}
    )


def test_project_id_is_never_a_saved_input() -> None:
    # Staging 2026-11-25: a config saved with project_id could not be started at all.
    with pytest.raises(WorkflowInputError, match="bound by Tin"):
        normalize_workflow_inputs(
            schema=_schema(), project_id=uuid4(), inputs={"project_id": str(uuid4())}
        )
