from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tin_lite import organic_system
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.community import discover
from tin_lite.domain import CONTENT_DIAGRAM_WORKFLOW_NAME
from tin_lite.private_workflows import authoring_guide, validate_private_definition
from tin_lite.procedures import PinnedCodexProcedure, validate_procedure_artifact
from tin_lite.public_workflows import PUBLIC_WORKFLOWS
from tin_lite.workflow_creator import creator_files
from tin_lite.workflow_diagrams import (
    PRESENTATION_PENDING,
    check_presentation,
    diagram_for_saved_workflow,
    validate_presentation,
    validate_workflow_diagram,
)
from tin_lite.workflow_packages import decode_workflow_source


def saveable(workflow) -> bool:
    """A workflow a person can add to My system: not a task and not agent-only."""
    return workflow.kind == "workflow" and not workflow.agent_only


def test_every_saveable_builtin_draws_how_it_runs_top_to_bottom() -> None:
    for workflow in BUILTIN_WORKFLOWS:
        if not saveable(workflow):
            continue
        if workflow.key in PRESENTATION_PENDING:
            assert workflow.presentation is None, f"{workflow.key} is drawn; drop it from pending"
            continue
        assert workflow.presentation is not None, f"{workflow.key} needs a presentation"
        flow = workflow.definition["presentation"]["flow"]
        validate_workflow_diagram(flow)
        assert flow["direction"] == "TD", workflow.key


def test_every_package_draws_how_it_runs_or_waits_for_the_backfill() -> None:
    for package in discover():
        manifest = json.loads((package.path / "workflow.json").read_text())
        definition = manifest["definition"]
        if package.key in PRESENTATION_PENDING:
            assert "presentation" not in definition, f"{package.key} is drawn; drop it from pending"
            continue
        validate_presentation(definition["presentation"])
        assert definition["presentation"]["flow"]["direction"] == "TD", package.key


def test_presentation_pending_names_only_real_workflows() -> None:
    keys = {workflow.key for workflow in BUILTIN_WORKFLOWS} | {item.key for item in discover()}
    assert PRESENTATION_PENDING <= keys


def test_the_organic_system_draws_every_step_it_runs() -> None:
    """A step added to the system's recipe must appear in its drawing."""
    workflow = next(item for item in BUILTIN_WORKFLOWS if item.key == organic_system.KEY)
    nodes = {node["id"] for node in workflow.definition["presentation"]["flow"]["nodes"]}
    assert set(organic_system.POLICY["steps"]) <= nodes


def test_an_approval_is_drawn_as_a_gate_whose_outcomes_are_signals() -> None:
    for workflow in BUILTIN_WORKFLOWS:
        if not workflow.presentation:
            continue
        flow = workflow.definition["presentation"]["flow"]
        gates = {node["id"] for node in flow["nodes"] if node["kind"] == "gate"}
        for edge in flow["edges"]:
            if edge["from"] in gates:
                assert edge["kind"] == "signal", (workflow.key, edge)


def test_a_registry_package_can_carry_a_presentation_flow() -> None:
    package = next(item for item in PUBLIC_WORKFLOWS if item.key == "outreach.community_threads")
    presentation = package.definition["presentation"]
    validate_presentation(presentation)
    assert [node["kind"] for node in presentation["flow"]["nodes"]] == [
        "store",
        "surface",
        "step",
        "store",
        "gate",
        "receipt",
    ]


def test_a_drawing_is_required_where_tin_controls_the_authoring() -> None:
    with pytest.raises(ValueError, match="presentation is required"):
        check_presentation({"key": "custom.new_report"}, required=True)
    check_presentation({"key": "custom.new_report"}, required=False)
    check_presentation({"key": next(iter(PRESENTATION_PENDING))}, required=True)
    with pytest.raises(ValueError):
        check_presentation(
            {"key": "custom.new_report", "presentation": {"flow": {}}}, required=False
        )


def test_code_and_private_definitions_accept_a_valid_drawing_only() -> None:
    guide = authoring_guide(settings=SimpleNamespace(), project_id=uuid4())
    for group in ("example_files", "code_example_files", "model_example_files"):
        for path, text in guide[group].items():
            if not path.endswith("workflow.json"):
                continue
            definition = decode_workflow_source(text.encode(), definition_path=path).definition
            assert definition["presentation"]["flow"]["direction"] == "TD"
            validate_private_definition(definition)
            broken = deepcopy(definition)
            broken["presentation"]["flow"]["nodes"] = broken["presentation"]["flow"]["nodes"][:1]
            with pytest.raises(ValueError):
                validate_private_definition(broken)


def test_the_creator_draws_its_candidates_and_itself() -> None:
    files = creator_files()
    manifest = "workflow_packages/custom.workflow_create/workflow.json"
    definition = decode_workflow_source(
        files[manifest].encode(), definition_path=manifest
    ).definition
    validate_private_definition(definition)
    assert definition["presentation"]["flow"]["nodes"][-1]["kind"] == "receipt"
    skill = files["workflow_packages/custom.workflow_create/skills/create-workflow/SKILL.md"]
    contract = files["workflow_packages/custom.workflow_create/skills/create-workflow/CONTRACT.md"]
    assert "presentation.flow" in skill and "presentation" in contract


SAVED = {"key": "content.generate", "version": "1.15.0", "executor": "codex.procedure"}
FLOW = {"direction": "TD", "nodes": [], "edges": []}


def test_a_saved_workflow_is_drawn_from_its_pin_when_the_pin_has_a_drawing() -> None:
    pinned = {**SAVED, "version": "1.12.0", "presentation": {"flow": {**FLOW, "pinned": True}}}
    current = {**SAVED, "presentation": {"flow": FLOW}}
    assert diagram_for_saved_workflow(current=current, pinned=pinned) == {
        "flow": {**FLOW, "pinned": True},
        "version": "1.12.0",
        "pinned_version": "1.12.0",
        "exact": True,
    }


def test_a_pin_from_before_the_backfill_borrows_todays_exact_drawing() -> None:
    current = {**SAVED, "presentation": {"flow": FLOW}}
    assert diagram_for_saved_workflow(current=current, pinned=dict(SAVED)) == {
        "flow": FLOW,
        "version": "1.15.0",
        "pinned_version": "1.15.0",
        "exact": True,
    }


def test_an_older_pin_is_drawn_from_today_and_says_so() -> None:
    current = {**SAVED, "presentation": {"flow": FLOW}}
    older = {**SAVED, "version": "1.12.0", "description": "before"}
    assert diagram_for_saved_workflow(current=current, pinned=older) == {
        "flow": FLOW,
        "version": "1.15.0",
        "pinned_version": "1.12.0",
        "exact": False,
    }
    unreadable = diagram_for_saved_workflow(current=current, pinned=None)
    assert unreadable["exact"] is False and unreadable["pinned_version"] is None


@pytest.mark.parametrize(
    "presentation",
    [
        None,
        {},
        {"flow": None},
        {"flow": {"direction": "TD", "nodes": [], "edges": []}},
        {"flow": {"direction": "TD", "nodes": [], "edges": []}, "theme": "dark"},
    ],
)
def test_presentation_holds_one_valid_flow(presentation: object) -> None:
    with pytest.raises(ValueError):
        validate_presentation(presentation)


def test_workflow_diagram_rejects_disconnected_presentation_nodes() -> None:
    with pytest.raises(ValueError, match="connected"):
        validate_workflow_diagram(
            {
                "direction": "LR",
                "nodes": [
                    {"id": "one", "kind": "step", "label": "one"},
                    {"id": "two", "kind": "step", "label": "two"},
                    {"id": "lost", "kind": "ghost", "label": "lost"},
                ],
                "edges": [{"from": "one", "to": "two", "kind": "call"}],
            }
        )


def test_content_diagram_resolves_one_safe_project_path_and_validates_source() -> None:
    workflow = next(item for item in BUILTIN_WORKFLOWS if item.key == CONTENT_DIAGRAM_WORKFLOW_NAME)
    procedure = workflow.procedure
    assert procedure is not None
    definition, _files = workflow.definition_and_resource_files()
    assert definition["procedure"]["output"]["path_template"] == "diagrams/{slug}.mmd"

    pinned = PinnedCodexProcedure(
        workflow_key=CONTENT_DIAGRAM_WORKFLOW_NAME,
        prompt="Draw it.",
        entry_skill="content-diagram",
        skill_files={"content-diagram/SKILL.md": b"instructions"},
        output_path_template="diagrams/{slug}.mmd",
        output_media_type="text/vnd.mermaid",
        output_validator="tin-diagram.v1",
        output_max_bytes=64_000,
    ).resolve_inputs({"slug": "how-tin-runs"})
    assert pinned.output_path == "diagrams/how-tin-runs.mmd"
    validate_procedure_artifact(
        b'graph LR\n  ask["founder asks<br/>one question"]:::step\n'
        b'  answer["receipt<br/>durable result"]:::receipt\n  ask --> answer\n',
        spec=pinned,
    )


@pytest.mark.parametrize("slug", ["../secret", "Too-Loud", "a/b", ""])
def test_content_diagram_rejects_unsafe_output_slugs(slug: str) -> None:
    procedure = PinnedCodexProcedure(
        workflow_key=CONTENT_DIAGRAM_WORKFLOW_NAME,
        prompt="Draw it.",
        entry_skill="content-diagram",
        skill_files={},
        output_path_template="diagrams/{slug}.mmd",
    )
    with pytest.raises(ValueError, match="slug"):
        procedure.resolve_inputs({"slug": slug})


def test_content_diagram_rejects_mermaid_outside_tin_vocabulary() -> None:
    procedure = PinnedCodexProcedure(
        workflow_key=CONTENT_DIAGRAM_WORKFLOW_NAME,
        prompt="Draw it.",
        entry_skill="content-diagram",
        skill_files={},
        output_path="diagrams/unsafe.mmd",
        output_media_type="text/vnd.mermaid",
        output_validator="tin-diagram.v1",
    )
    with pytest.raises(ValueError, match="outside"):
        validate_procedure_artifact(
            b"graph LR\n  A[unsafe] --> B[unsafe]\n  click A javascript:alert(1)\n",
            spec=procedure,
        )
    with pytest.raises(ValueError, match="node label"):
        validate_procedure_artifact(
            b'graph LR\n  one["&lt;script&gt;"]:::step\n  two["done"]:::receipt\n  one --> two\n',
            spec=procedure,
        )
