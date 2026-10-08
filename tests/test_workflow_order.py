"""Workflows within a dashboard group follow WORKFLOW_DISPLAY_ORDER, then key."""

from __future__ import annotations

from test_procedure_publication import publication_db as publication_db

from tin_lite.catalog import BUILTIN_WORKFLOWS, ORGANIC_TRAFFIC_SYSTEM, WORKFLOW_SYSTEMS
from tin_lite.public_workflows import PUBLIC_WORKFLOWS
from tin_lite.workflow_order import WORKFLOW_DISPLAY_ORDER


def _catalog():
    """Every registered workflow as the arguments upsert_registry_workflow takes."""
    for item in BUILTIN_WORKFLOWS:
        yield (
            item.id,
            item.key,
            item.title,
            item.description,
            item.executor,
            item.definition_path,
            item.version_label,
            item.definition,
        )
    for item in PUBLIC_WORKFLOWS:
        definition = item.definition
        yield (
            item.id,
            item.key,
            definition["title"],
            definition["description"],
            definition["executor"],
            item.definition_path,
            definition.get("version", "1.0.0"),
            definition,
        )


def _listed(definition) -> bool:
    return not definition.get("agent_only") and definition.get("public_discovery", True)


def test_display_order_names_registered_listed_workflows_once() -> None:
    definitions = {key: definition for _, key, *_, definition in _catalog()}

    assert len(WORKFLOW_DISPLAY_ORDER) == len(set(WORKFLOW_DISPLAY_ORDER))
    for key in WORKFLOW_DISPLAY_ORDER:
        assert key in definitions, f"{key} is not registered"
        assert _listed(definitions[key]), f"{key} is hidden from the catalog"


def test_every_listed_organic_workflow_has_a_place() -> None:
    organic = {
        key
        for _, key, *_, definition in _catalog()
        if definition.get("system") == ORGANIC_TRAFFIC_SYSTEM and _listed(definition)
    }

    assert organic == {key for key in WORKFLOW_DISPLAY_ORDER if key in organic}
    assert organic <= set(WORKFLOW_DISPLAY_ORDER)


async def test_list_workflows_orders_a_group_by_display_order_then_key(publication_db) -> None:
    for system in WORKFLOW_SYSTEMS:
        await publication_db.upsert_workflow_system(
            system_id=system.id, name=system.name, display_order=system.display_order
        )
    for workflow_id, key, title, description, executor, path, version, definition in _catalog():
        await publication_db.upsert_registry_workflow(
            workflow_id=workflow_id,
            key=key,
            title=title,
            description=description,
            executor=executor,
            definition_repo_id="registry/workflows",
            definition_path=path,
            current_commit_sha="a" * 40,
            version_label=version,
            definition=definition,
        )

    listed = await publication_db.list_workflows()
    organic = [item.key for item in listed if item.system_id == ORGANIC_TRAFFIC_SYSTEM]
    ordered = [key for key in WORKFLOW_DISPLAY_ORDER if key in organic]

    assert organic[: len(ordered)] == ordered
    assert organic[len(ordered) :] == sorted(organic[len(ordered) :])
    # Groups without an entry keep sorting by key.
    social = [item.key for item in listed if item.system_id == "x"]
    assert social == sorted(social)
