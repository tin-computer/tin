"""Code consumers pin confirmed authors and read their guide from the run's file snapshot."""

import base64
import copy
import json
from uuid import UUID, uuid4

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from temporalio.testing import ActivityEnvironment
from test_billing import billed as billed
from test_private_workflows import ACTOR, structured
from test_procedure_publication import publication_db as publication_db
from test_workflow_code import PATH, SyntheticCompute, activate_code, example_files, setup, start

from tin_lite import code_project_files, project_authors
from tin_lite.private_workflows import validate_private_definition


def authored():
    package = json.loads(example_files()[PATH])
    definition = package["definition"]
    definition["code"]["author"] = True
    definition["input_schema"]["properties"]["author_id"] = {
        "type": "string",
        "format": "uuid",
        "x-tin-ui": {"control": "project_author"},
    }
    definition["input_schema"]["required"].append("author_id")
    return package


def test_author_capability_is_explicit_and_typed():
    definition = authored()["definition"]
    validate_private_definition(definition)
    for change in (
        lambda d: d["code"].update(author={"resolver": "custom"}),
        lambda d: d["input_schema"]["required"].remove("author_id"),
        lambda d: d["input_schema"]["properties"]["author_id"].pop("format"),
    ):
        invalid = copy.deepcopy(definition)
        change(invalid)
        with pytest.raises(ValueError):
            validate_private_definition(invalid)


async def test_author_and_guide_remain_pinned_after_binding_and_file_edits(billed, monkeypatch):
    f = billed
    observed = []

    class Compute(SyntheticCompute):
        async def run_code_and_kill(self, *, packet, model_call, **kwargs):
            author = packet["context"]["author"]
            assert set(author) == {"id", "display_name", "guide_path", "version"}
            guide = base64.b64decode(
                await model_call(
                    {"kind": "file", "operation": "read_text", "path": author["guide_path"]}
                )
            ).decode()
            observed.append((author, guide))
            return await super().run_code_and_kill(packet=packet, **kwargs)

    server, _, code = await setup(f, monkeypatch, compute=Compute())
    revision = f.storage.repo.edit(
        {PATH: json.dumps(authored()).encode(), "style/one.md": b"Original voice"}
    )
    active = await activate_code(f, server, revision=revision)
    service = project_authors.ProjectAuthors(f.db, f.storage)
    author_id = uuid4()
    author = await service.save(
        project_id=f.project.id,
        actor=ACTOR,
        author_id=author_id,
        display_name="Alex",
        expected_version=0,
        selected_guide="style/one.md",
    )
    inputs = {"author_id": str(author_id), "minimum_cents": 1}
    setup_result = structured(
        await server.call_tool(
            "get_code_workflow_setup",
            {
                "project_id": str(f.project.id),
                "workflow_id": active["workflow_id"],
                "inputs": inputs,
            },
        )
    )
    assert setup_result["author"]["guide_path"] == "style/one.md"
    for invalid in (
        {**inputs, "author_id": str(uuid4())},
        {**inputs, "style_path": "another-author.md"},
    ):
        with pytest.raises(ToolError):
            await start(f, server, active, inputs=invalid)
    key = str(uuid4())
    result = await start(f, server, active, inputs=inputs, request_id=key)
    run = await f.db.get_run(UUID(result["id"]))
    f.storage.repo.edit({"style/one.md": b"Edited voice", "style/two.md": b"Replacement voice"})
    await service.save(
        project_id=f.project.id,
        actor=ACTOR,
        author_id=author_id,
        display_name="Renamed",
        expected_version=1,
        selected_guide="style/two.md",
    )
    assert (await start(f, server, active, inputs=inputs, request_id=key))["id"] == result["id"]
    assert (await project_authors.saved_author(f.db, run))["guide_path"] == author["guide_path"]
    await ActivityEnvironment().run(code.execute, result["id"])
    assert observed[0][0]["display_name"] == "Alex"
    assert observed[0][1] == "Original voice"


async def test_artifact_checks_use_the_code_run_snapshot_when_head_moves(billed, monkeypatch):
    f = billed
    server, _, _ = await setup(f, monkeypatch)
    package = json.loads(example_files()[PATH])
    package["definition"]["prerequisites"] = [
        {
            "kind": "artifact",
            "path": "notes/input.md",
            "level": "required",
            "reason": "Read the chosen file.",
        }
    ]
    revision = f.storage.repo.edit(
        {PATH: json.dumps(package).encode(), "notes/input.md": b"Present at admission"}
    )
    active = await activate_code(f, server, revision=revision)
    original_select = code_project_files.select

    async def select(**kwargs):
        source = await original_select(**kwargs)
        # Simulate another canonical commit after selecting the run's input snapshot.
        f.storage.repo.edit({"notes/input.md": None})
        return source

    monkeypatch.setattr(code_project_files, "select", select)
    result = await start(f, server, active)
    run = await f.db.get_run(UUID(result["id"]))
    source = await code_project_files.saved_source(f.db, run, f.project)
    item = run.prerequisite_evidence["items"][0]
    assert item["satisfied"] and item["evidence"]["revision"] == source["revision"] == revision
