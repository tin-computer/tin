import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from test_procedure_publication import publication_db as publication_db

from tin_lite import catalog, organic_system
from tin_lite.system_wiki import SystemWikiRef


class RegistrySnapshots:
    def __init__(self):
        self.revisions = {}
        self.head = None

    async def publish_workflow_files(self, *, files, known_commit_sha, **_):
        for revision in (known_commit_sha, self.head):
            if revision and all(
                self.revisions[revision].get(path) == content for path, content in files.items()
            ):
                return revision
        revision = f"{len(self.revisions) + 1:040x}"
        self.revisions[revision] = {**self.revisions.get(self.head, {}), **files}
        self.head = revision
        return revision


def catalog_database():
    rows = {}

    async def save(**values):
        rows[values["workflow_id"]] = SimpleNamespace(project_id=None, **values)

    return SimpleNamespace(
        rows=rows,
        get_workflow=AsyncMock(side_effect=lambda workflow_id: rows.get(workflow_id)),
        upsert_workflow_system=AsyncMock(),
        upsert_registry_workflow=AsyncMock(side_effect=save),
    )


WIKI = SystemWikiRef("wiki/system", "growth/project-scanning.md", "a" * 40)


async def test_parent_publication_pins_children_on_first_sync_and_child_only_upgrade(monkeypatch):
    db, storage = catalog_database(), RegistrySnapshots()
    parent = next(item for item in catalog.BUILTIN_WORKFLOWS if item.key == organic_system.KEY)
    assert catalog.BUILTIN_WORKFLOWS[0] == parent  # Deliberately before its children.
    await catalog.sync_builtin_workflows(database=db, storage=storage, system_wiki=WIKI)
    first = db.rows[parent.id].current_commit_sha
    for child in catalog.BUILTIN_WORKFLOWS:
        if child.key not in (*organic_system.STEPS.values(), organic_system.REFRESH_KEY):
            continue
        definition, resources = child.definition_and_files_with_wiki(WIKI)
        assert json.loads(storage.revisions[first][child.definition_path]) == definition
        assert all(storage.revisions[first][p] == value for p, value in resources.items())
    await catalog.sync_builtin_workflows(database=db, storage=storage, system_wiki=WIKI)
    assert db.rows[parent.id].current_commit_sha == first

    child = next(item for item in catalog.BUILTIN_WORKFLOWS if item.key == "organic.audit")
    upgraded = replace(child, version_label="test-upgrade", title="New child title")
    monkeypatch.setattr(
        catalog,
        "BUILTIN_WORKFLOWS",
        tuple(upgraded if item == child else item for item in catalog.BUILTIN_WORKFLOWS),
    )
    await catalog.sync_builtin_workflows(database=db, storage=storage, system_wiki=WIKI)
    second = db.rows[parent.id].current_commit_sha
    assert second != first
    assert json.loads(storage.revisions[second][child.definition_path])["title"] == upgraded.title
    assert json.loads(storage.revisions[first][child.definition_path])["title"] == child.title


async def test_catalog_collision_fails_before_any_registry_or_projection_write():
    db, storage = catalog_database(), RegistrySnapshots()
    builtin = catalog.BUILTIN_WORKFLOWS[-1]
    db.rows[builtin.id] = SimpleNamespace(project_id=None, key="another.workflow")
    with pytest.raises(RuntimeError, match="different workflow"):
        await catalog.sync_builtin_workflows(database=db, storage=storage, system_wiki=WIKI)
    assert not storage.revisions
    db.upsert_workflow_system.assert_not_awaited()
    db.upsert_registry_workflow.assert_not_awaited()


async def test_database_cannot_repurpose_a_published_builtin_id(publication_db):
    # ID 26 is already owned by the separately deployed character workflow.
    values = dict(
        workflow_id=UUID("00000000-0000-4000-8000-000000000026"),
        key="creative.character_direct",
        title="Existing character workflow",
        description="Existing production contract",
        executor="creative.character_direct",
        definition_repo_id="registry/workflows",
        definition_path="workflows/creative.character_direct.json",
        current_commit_sha="a" * 40,
        version_label="1.0.0",
        definition={},
    )
    await publication_db.upsert_registry_workflow(**values)
    with pytest.raises(RuntimeError, match="different workflow"):
        await publication_db.upsert_registry_workflow(**{**values, "key": "organic.technical_fix"})
    saved = await publication_db.get_workflow(values["workflow_id"])
    assert saved.key == values["key"] and saved.current_commit_sha == "a" * 40
    repaired = next(
        item for item in catalog.BUILTIN_WORKFLOWS if item.key == "organic.technical_fix"
    )
    assert repaired.id == UUID("00000000-0000-4000-8000-000000000028")
    await publication_db.upsert_registry_workflow(**{**values, "title": "Updated same workflow"})
    assert (
        await publication_db.get_workflow(values["workflow_id"])
    ).title == "Updated same workflow"


async def test_x_parent_pins_native_and_package_children_together():
    from tin_lite import x_draft, x_style
    from tin_lite.workflow_packages import decode_workflow_source

    db, storage = catalog_database(), RegistrySnapshots()
    await catalog.sync_builtin_workflows(database=db, storage=storage, system_wiki=WIKI)
    parent = db.rows[x_draft.WORKFLOW_ID]
    files = storage.revisions[parent.current_commit_sha]
    assert json.loads(files[f"workflows/{x_style.KEY}.json"])["executor"] == x_style.KEY
    path = "workflow_packages/social.x_compose/workflow.json"
    source = decode_workflow_source(files[path], definition_path=path)
    assert source.definition["key"] == "social.x_compose"
    assert all(path in files for path in source.resource_paths.values())
    assert b"async def run(" in files["workflow_packages/social.x_compose/main.py"]
