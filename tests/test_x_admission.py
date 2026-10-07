"""X delivery can start only for the exact confirmed post and approved actor."""

import json
from uuid import uuid4

import pytest
from test_private_workflows import ACTOR
from test_private_workflows import fixture as project_fixture
from test_procedure_publication import publication_db as publication_db

from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.run_service import start_workflow_run
from tin_lite.workflow_inputs import WorkflowInputError
from tin_lite.x_posts import KEY, save_effect


async def fixture(db):
    f = await project_fixture(db)
    await db.upsert_workflow_system(
        system_id="organic-traffic", name="Organic traffic", display_order=1
    )
    builtin = next(item for item in BUILTIN_WORKFLOWS if item.key == KEY)
    definition = builtin.definition
    revision = "d" * 40
    await db.upsert_registry_workflow(
        workflow_id=builtin.id,
        key=KEY,
        title=builtin.title,
        description=builtin.description,
        executor=KEY,
        definition_repo_id="registry/workflows",
        definition_path=builtin.definition_path,
        current_commit_sha=revision,
        version_label=builtin.version_label,
        definition=definition,
    )
    f.workflow = await db.get_workflow(builtin.id)
    original_read = f.storage.read_canonical_artifact

    async def read(**kwargs):
        if kwargs["repo_id"] == "registry/workflows":
            assert kwargs["commit_sha"] == revision
            assert kwargs["path"] == builtin.definition_path
            return json.dumps(definition).encode()
        return await original_read(**kwargs)

    f.storage.read_canonical_artifact = read
    return f


async def start(f, approval_id, *, actor=ACTOR, key=None, internal=False):
    return await start_workflow_run(
        runtime=f.runtime,
        settings=f.settings,
        workflow=f.workflow,
        project_id=f.project.id,
        started_by_clerk_user_id=actor,
        start_idempotency_key=key or f"x-publish:{approval_id}",
        input_payload={"approval_id": str(approval_id)},
        _x_publication=internal,
    )


async def test_x_publish_requires_confirmed_actor_and_internal_start(publication_db):
    f = await fixture(publication_db)
    approval_id = uuid4()
    with pytest.raises(WorkflowInputError, match="confirm publication"):
        await start(f, approval_id, internal=True)
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 0
    await save_effect(
        f.db,
        f"x:approved:{approval_id}",
        {"project_id": str(f.project.id), "actor": ACTOR},
    )
    with pytest.raises(WorkflowInputError, match="confirm publication"):
        await start(f, approval_id)
    with pytest.raises(WorkflowInputError, match="confirm publication"):
        await start(f, approval_id, key=f"x-publish:{uuid4()}", internal=True)
    other = "user_otherxmember"
    await f.db.record_tin_user(other)
    await f.db.grant_project_membership(project_id=f.project.id, clerk_user_id=other)
    with pytest.raises(WorkflowInputError, match="different approval request"):
        await start(f, approval_id, actor=other, internal=True)
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 0
    run = await start(f, approval_id, internal=True)
    same = await start(f, approval_id, internal=True)
    assert same.id == run.id
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM workflow_runs WHERE project_id=$1 AND executor=$2",
            f.project.id,
            KEY,
        )
        == 1
    )


async def test_x_publish_rejects_sibling_project_approval(publication_db):
    f = await fixture(publication_db)
    approval_id = uuid4()
    await save_effect(
        f.db,
        f"x:approved:{approval_id}",
        {"project_id": str(uuid4()), "actor": ACTOR},
    )
    with pytest.raises(WorkflowInputError, match="confirm publication"):
        await start(f, approval_id, internal=True)
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 0
