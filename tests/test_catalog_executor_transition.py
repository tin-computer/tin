from dataclasses import replace

import pytest
from test_checkpoint_contract import fixture_state
from test_procedure_publication import publication_db as publication_db

from tin_lite import growth_plan, organic_system
from tin_lite.catalog import (
    BUILTIN_WORKFLOWS,
    EXECUTOR_TRANSITIONS,
    executor_replaced_by,
    sync_builtin_workflows,
)
from tin_lite.domain import CODEX_PROCEDURE_EXECUTOR
from tin_lite.public_workflows import PUBLIC_WORKFLOWS
from tin_lite.system_wiki import SystemWikiRef

WIKI = SystemWikiRef("wiki/system", "growth/project-scanning.md", "w" * 40)


class PublishedCatalog:
    """A database that already holds every built-in, published, as an earlier release left it."""

    def __init__(self, published_executors):
        _, self.workflow, _ = fixture_state()
        self.published_executors = published_executors
        self.synced = {}

    async def upsert_workflow_system(self, **values):
        pass

    async def get_workflow(self, workflow_id):
        builtin = next(
            item for item in (*BUILTIN_WORKFLOWS, *PUBLIC_WORKFLOWS) if item.id == workflow_id
        )
        return replace(
            self.workflow,
            id=workflow_id,
            key=builtin.key,
            executor=self.published_executors.get(builtin.key, builtin.executor),
            definition_path=builtin.definition_path,
        )

    async def upsert_registry_workflow(self, **values):
        self.synced[values["key"]] = values


class Storage:
    async def publish_workflow_files(self, **values):
        return values["known_commit_sha"]


async def test_the_plan_moves_from_the_codex_procedure_and_nothing_else_may(monkeypatch):
    # The organic system pins its two measurement packages; no other package is needed.
    monkeypatch.setattr(
        "tin_lite.public_workflows.PUBLIC_WORKFLOWS",
        tuple(
            item for item in PUBLIC_WORKFLOWS if item.key in organic_system.MEASURE_STEPS.values()
        ),
    )
    # Executor migration is a native-only fixture; the X parent needs its code package.
    monkeypatch.setattr(
        "tin_lite.catalog.BUILTIN_WORKFLOWS",
        tuple(item for item in BUILTIN_WORKFLOWS if item.key != "social.x_draft"),
    )
    # The only reviewed transition: the Start here plan, from the Codex procedure it shipped as.
    assert EXECUTOR_TRANSITIONS == {growth_plan.KEY: (CODEX_PROCEDURE_EXECUTOR, growth_plan.KEY)}
    assert executor_replaced_by(growth_plan.KEY, growth_plan.KEY) == CODEX_PROCEDURE_EXECUTOR
    assert executor_replaced_by(growth_plan.KEY, "workflow.code") is None
    assert executor_replaced_by("content.plan", "content.plan") is None

    # A production database still holds the plan as a Codex procedure; startup must not refuse it.
    database = PublishedCatalog({growth_plan.KEY: CODEX_PROCEDURE_EXECUTOR})
    await sync_builtin_workflows(database=database, storage=Storage(), system_wiki=WIKI)
    assert database.synced[growth_plan.KEY]["executor"] == growth_plan.KEY
    assert database.synced[growth_plan.KEY]["replaces_executor"] == CODEX_PROCEDURE_EXECUTOR
    assert database.synced["content.plan"]["replaces_executor"] is None

    # Any other published executor, for the plan or for another workflow, still stops startup.
    for published in ({growth_plan.KEY: "workflow.code"}, {"content.plan": "codex.procedure"}):
        with pytest.raises(RuntimeError, match="executor and source location cannot change"):
            await sync_builtin_workflows(
                database=PublishedCatalog(published), storage=Storage(), system_wiki=WIKI
            )


async def test_the_registry_row_changes_executor_only_from_the_named_one(publication_db):
    db = publication_db
    spec = next(item for item in BUILTIN_WORKFLOWS if item.key == growth_plan.KEY)
    values = dict(
        workflow_id=spec.id,
        key=spec.key,
        title=spec.title,
        description=spec.description,
        definition_repo_id="registry/workflows",
        definition_path=spec.definition_path,
        version_label=spec.version_label,
        definition={"key": spec.key},
    )
    await db.upsert_registry_workflow(
        **values, executor=CODEX_PROCEDURE_EXECUTOR, current_commit_sha="c" * 40
    )

    with pytest.raises(RuntimeError, match="different workflow or source"):
        await db.upsert_registry_workflow(
            **values, executor=spec.executor, current_commit_sha="d" * 40
        )
    with pytest.raises(RuntimeError, match="different workflow or source"):
        await db.upsert_registry_workflow(
            **values,
            executor=spec.executor,
            current_commit_sha="d" * 40,
            replaces_executor="workflow.code",
        )

    moved = await db.upsert_registry_workflow(
        **values,
        executor=spec.executor,
        current_commit_sha="d" * 40,
        replaces_executor=CODEX_PROCEDURE_EXECUTOR,
    )
    assert (moved.executor, moved.current_commit_sha) == (growth_plan.KEY, "d" * 40)
