from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tin_lite import growth_onboarding
from tin_lite.catalog import (
    BUILTIN_WORKFLOWS,
    CREATIVE_STUDIO_SYSTEM,
    RETIRED_BUILTIN_WORKFLOW_IDS,
)
from tin_lite.domain import (
    CREATIVE_LAUNCH_VIDEO_WORKFLOW_NAME,
    CREATIVE_PRODUCT_DEMO_WORKFLOW_NAME,
)
from tin_lite.launch_video import MODEL_ROUTE, SYSTEM, model_route_definition
from tin_lite.mcp_public import PublicMCPServer, published_workflows
from tin_lite.workflow_diagrams import validate_presentation
from tin_lite.workflow_inputs import WorkflowInputError, normalize_workflow_inputs
from tin_lite.workflow_prerequisites import parse_workflow_prerequisites

LAUNCH = next(w for w in BUILTIN_WORKFLOWS if w.key == CREATIVE_LAUNCH_VIDEO_WORKFLOW_NAME)
DEMO = next(w for w in BUILTIN_WORKFLOWS if w.key == CREATIVE_PRODUCT_DEMO_WORKFLOW_NAME)
PROJECT_ID = UUID("00000000-0000-4000-8000-0000000000aa")
INPUTS = {
    "slug": "one-useful-result",
    "feature_moment": "Follow a local draft into the user's translated product screen.",
    "platform": "X",
    "format": "landscape",
    "duration_seconds": 24,
    "sound_mode": "music",
    "music_path": "music/cleared.wav",
    "music_license": "Owned original recording; commercial use approved.",
}


def normalize(inputs: dict) -> dict:
    return normalize_workflow_inputs(
        schema=LAUNCH.definition["input_schema"], project_id=PROJECT_ID, inputs=inputs
    )


def test_launch_is_separate_native_anthropic_workflow_with_its_own_review() -> None:
    assert LAUNCH.id == UUID("00000000-0000-4000-8000-000000000048")
    assert LAUNCH.id not in RETIRED_BUILTIN_WORKFLOW_IDS
    assert len({w.id for w in BUILTIN_WORKFLOWS}) == len(BUILTIN_WORKFLOWS)
    definition, files = LAUNCH.definition_and_resource_files()
    assert definition["executor"] == CREATIVE_LAUNCH_VIDEO_WORKFLOW_NAME
    assert definition["system"] == CREATIVE_STUDIO_SYSTEM
    assert definition["schedule_modes"] == ["on_demand"]
    assert definition["model_route"] == model_route_definition()
    assert MODEL_ROUTE.provider == "anthropic"
    assert "procedure" not in definition
    assert not files
    assert definition["human_review"]["review_label"] == "Review launch film"
    assert definition["human_review"] != DEMO.definition["human_review"]
    validate_presentation(definition["presentation"])


@pytest.mark.parametrize("field", list(INPUTS))
def test_launch_requires_explicit_brief_and_delivery_choices(field) -> None:
    inputs = {key: value for key, value in INPUTS.items() if key != field}
    with pytest.raises(WorkflowInputError, match="required property"):
        normalize(inputs)


@pytest.mark.parametrize("format", ["landscape", "portrait", "square"])
def test_launch_accepts_each_explicit_format_without_choosing_its_duration(format) -> None:
    result = normalize({**INPUTS, "format": format})
    assert result["format"] == format
    assert result["duration_seconds"] == 24
    for field in ("platform", "format", "duration_seconds", "sound_mode", "captions"):
        assert "default" not in LAUNCH.definition["input_schema"]["properties"][field]
    assert "product_url" not in result


@pytest.mark.parametrize(
    "change",
    [
        {"duration_seconds": 7},
        {"duration_seconds": 91},
        {"duration_seconds": 20.5},
        {"duration_seconds": True},
        {"format": "horizontal"},
        {"sound_mode": "narration"},
        {"slug": "../other"},
        {"feature_moment": ""},
        {"platform": ""},
        {"product_name": "Repeat existing project context"},
    ],
)
def test_launch_rejects_unusable_briefs(change) -> None:
    with pytest.raises(WorkflowInputError):
        normalize({**INPUTS, **change})


@pytest.mark.parametrize("seconds", [8, 90])
def test_launch_accepts_duration_boundaries(seconds) -> None:
    assert normalize({**INPUTS, "duration_seconds": seconds})["duration_seconds"] == seconds


def test_narration_requires_and_preserves_the_caption_choice() -> None:
    with pytest.raises(WorkflowInputError, match="captions.*required"):
        normalize({**INPUTS, "sound_mode": "voice_and_music"})
    for captions in (False, True):
        result = normalize({**INPUTS, "sound_mode": "voice_and_music", "captions": captions})
        assert result["captions"] is captions
    assert normalize({**INPUTS, "sound_mode": "silent"})["sound_mode"] == "silent"


def test_story_preferences_and_prior_answers_survive_normalization() -> None:
    answers = {
        **INPUTS,
        "story_preference": "User chose a direct feature demonstration.",
        "filming_preference": "User chose one continuous close shot.",
        "notes": "The agent may choose the track. Keep the user's plain ending without a CTA.",
    }
    assert normalize(answers).items() >= answers.items()
    assert normalize(INPUTS)["story_preference"] == ""
    assert normalize(INPUTS)["filming_preference"] == ""


def test_launch_reads_project_evidence_without_requiring_a_mascot_or_retyped_context() -> None:
    definition = LAUNCH.definition
    prerequisites = parse_workflow_prerequisites(
        definition["prerequisites"], input_schema=definition["input_schema"]
    )
    assert {item.path for item in prerequisites} == {"wiki/INDEX.md", "BRAND.md", "DESIGN.md"}
    assert all(item.level == "recommended" for item in prerequisites)
    assert next(item for item in prerequisites if item.section).section == "### Feature map"
    assert "integration_requirements" not in definition
    assert (
        not {"product_url", "product_name", "brand", "character"}
        & definition["input_schema"]["properties"].keys()
    )
    expectation = growth_onboarding.expectation(LAUNCH.key)
    assert "four brief questions" in expectation["first"]
    assert "launches/<slug>.mp4" in expectation["lands"]


async def test_public_launch_tool_exposes_the_brief_and_uses_the_shared_start_handler() -> None:
    assert published_workflows()[LAUNCH.id] is LAUNCH
    start = AsyncMock(return_value={"id": str(uuid4()), "status": "running"})
    database = SimpleNamespace(has_project_access=AsyncMock(return_value=True))
    server = PublicMCPServer("launch-fixture")
    server.configure(
        start=start,
        start_saved=AsyncMock(),
        caller=AsyncMock(return_value=SimpleNamespace(subject="fixture")),
        runtime=lambda: SimpleNamespace(database=database),
    )
    tool = next(tool for tool in await server.list_tools() if tool.name == "start_launch_video")
    assert "four short questions" in tool.description
    assert "before starting this background run" in tool.description.lower()
    assert tool.annotations.destructive_hint and tool.annotations.open_world_hint
    assert "project_id" not in tool.input_schema["properties"]["inputs"]["properties"]
    request_id = str(uuid4())
    result = await server.call_tool(
        "start_launch_video",
        {"project_id": str(PROJECT_ID), "request_id": request_id, "inputs": INPUTS},
    )
    assert not result.is_error
    start.assert_awaited_once_with(
        workflow_id=str(LAUNCH.id), project_id=str(PROJECT_ID), request_id=request_id, inputs=INPUTS
    )
    with pytest.raises(ToolError, match="invalid"):
        await server.call_tool(
            "start_launch_video",
            {
                "project_id": str(PROJECT_ID),
                "request_id": str(uuid4()),
                "inputs": {key: value for key, value in INPUTS.items() if key != "format"},
            },
        )
    assert start.await_count == 1


def test_launch_directions_remain_product_specific_and_do_not_leak_owner_notes() -> None:
    from pathlib import Path

    import tin_lite.launch_video as launch

    guide = Path(launch.__file__).with_name("launch_directions.md").read_text()
    text = SYSTEM + guide
    for phrase in (
        "illustrated example",
        "example session",
        "1 of 5",
        "supported product",
        "untrusted source data",
        "No eyebrow",
        "measured onset_times",
    ):
        assert phrase in text
    story, filming = guide.split("## Six filming approaches")
    filming, cinema = filming.split("## Three cinematic ideas to adapt")
    assert len([line for line in story.splitlines() if line.startswith("| ")]) == 10
    assert len([line for line in filming.splitlines() if line.startswith("| ")]) == 8
    assert len([line for line in cinema.splitlines() if line.startswith("| ")]) == 5
    assert "/Users/" not in text


def test_existing_product_demo_keeps_its_source_inputs_and_output_contract() -> None:
    definition = DEMO.definition
    assert DEMO.id == UUID("00000000-0000-4000-8000-000000000022")
    assert definition["version"] == "1.1.1"
    assert definition["procedure"]["entry_skill"] == "product-demo-video"
    assert definition["procedure"]["output"] == {
        "kind": "project.artifact",
        "path_template": "demos/{slug}.mp4",
        "media_type": "video/mp4",
        "max_bytes": 16_000_000,
        "validator": "demo-video.v1",
    }
    assert definition["input_schema"]["required"] == ["project_id", "slug", "product_url"]
    assert definition["human_review"]["queue_clause"] == "Demo video ready to finish"
