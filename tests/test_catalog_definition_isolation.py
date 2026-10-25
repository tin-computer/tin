from copy import deepcopy
from dataclasses import replace

import pytest

from tin_lite import style_capture
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.system_wiki import SystemWikiRef


def definition(builtin, view):
    if view == "property":
        return builtin.definition
    if view == "resources":
        return builtin.definition_and_resource_files()[0]
    return builtin.definition_and_files_with_wiki(
        SystemWikiRef("wiki/system", "guide.md", "a" * 40)
    )[0]


@pytest.mark.parametrize("view", ["property", "resources", "wiki"])
def test_definition_edits_do_not_change_the_catalog_template(view):
    original = next(item for item in BUILTIN_WORKFLOWS if item.key == "research.deep_dive")
    builtin = replace(
        original,
        input_schema=deepcopy(original.input_schema),
        default_schedule={"cadence": "weekly", "weekdays": ["monday"]},
    )
    expected = deepcopy(definition(builtin, view))
    edited = definition(builtin, view)
    edited["input_schema"]["properties"]["depth"]["default"] = "focused"
    edited["input_schema"]["properties"]["depth"]["enum"].append("another")
    edited["default_schedule"]["weekdays"].append("friday")

    assert builtin.input_schema == expected["input_schema"]
    assert builtin.default_schedule == expected["default_schedule"]
    assert definition(builtin, view) == expected


@pytest.mark.parametrize("view", ["property", "resources", "wiki"])
def test_definition_edits_do_not_change_shared_native_schemas(monkeypatch, view):
    builtin = next(item for item in BUILTIN_WORKFLOWS if item.key == style_capture.KEY)
    schema = deepcopy(style_capture.MODEL_SCHEMA)
    monkeypatch.setattr(style_capture, "MODEL_SCHEMA", schema)
    expected = deepcopy(definition(builtin, view))
    edited = definition(builtin, view)
    edited["style_schema"]["properties"]["test_only"] = {"type": "string"}

    assert schema == expected["style_schema"]
    assert definition(builtin, view) == expected
