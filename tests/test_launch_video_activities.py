from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from tin_lite import launch_video_activities as module
from tin_lite.domain import EffectReceipt, RunStatus
from tin_lite.launch_video import (
    KEY,
    LaunchVideoError,
    LaunchVideoOutputError,
    model_route_definition,
)
from tin_lite.launch_video_activities import LaunchVideoActivities, launch_path
from tin_lite.workflows import LaunchVideoWorkflow, registered_workflow_implementations


class Database:
    def __init__(self, run, project):
        self.run, self.project = run, project
        self.receipts = {}
        self.progress = []

    @asynccontextmanager
    async def effect_lock(self, key, operation):
        yield None, self.receipts.get(key)

    @asynccontextmanager
    async def project_state_lock(self, conn, project_id):
        yield

    async def start_effect(self, conn, *, execution_key, operation):
        self.receipts[execution_key] = EffectReceipt(execution_key, operation, "started", None)

    async def complete_effect(self, conn, *, execution_key, result):
        self.receipts[execution_key] = EffectReceipt(execution_key, "test", "completed", result)

    async def fail_effect(self, conn, *, execution_key, error_message):
        self.receipts[execution_key] = EffectReceipt(execution_key, "test", "failed", None)

    async def get_effect(self, key):
        return self.receipts.get(key)

    async def get_run(self, run_id):
        return self.run

    async def get_project(self, project_id):
        return self.project

    async def mark_run_running(self, run_id):
        self.run.status = RunStatus.RUNNING

    async def project_run_progress(self, **values):
        self.progress.append(values["step"])

    async def request_human_review(self, **values):
        self.run.review = values
        return True

    async def record_human_review(self, **values):
        self.run.approved = True

    async def project_success(self, **values):
        self.run.success = values

    async def project_failure(self, **values):
        self.run.failure = values


@pytest.fixture
def rig(monkeypatch):
    project = SimpleNamespace(
        id=uuid4(), name="Product QA", state_repo_id="projects/qa", canonical_branch="main"
    )
    run = SimpleNamespace(
        id=uuid4(),
        project_id=project.id,
        executor=KEY,
        definition_commit_sha="d" * 40,
        status=RunStatus.PENDING,
        input={
            "slug": "one-action",
            "sound_mode": "music",
            "music_path": "music/approved.mp3",
            "music_license": "CC0, approved by the user",
            "asset_paths": ["images/product.png"],
            "duration_seconds": 20,
            "format": "landscape",
        },
    )
    db = Database(run, project)
    documents = {
        "wiki/INDEX.md": b"# Memory\nA product action with a supported result",
        "BRAND.md": b"Brand rules",
        "DESIGN.md": None,
        "images/product.png": b"png bytes",
        "music/approved.mp3": b"approved music bytes",
        "a/same.png": b"png one",
        "b/same.png": b"png two",
    }

    async def read(*, repo_id, commit_sha, path, **_):
        if repo_id == "registry/workflows":
            return json.dumps({"model_route": model_route_definition()}).encode()
        assert commit_sha in {"c" * 40, "e" * 40}
        return documents[path]

    async def media(*, repo_id, revision, path):
        assert revision == "c" * 40
        return documents[path]

    async def publish(*, path, content, **_):
        documents[path] = content
        return "e" * 40, True

    storage = SimpleNamespace(
        get_repo=AsyncMock(return_value="repo"),
        head_sha=AsyncMock(return_value="c" * 40),
        read_canonical_artifact=AsyncMock(side_effect=read),
        read_bounded_project_file=AsyncMock(side_effect=read),
        read_project_media=AsyncMock(side_effect=media),
        publish_state_document=AsyncMock(side_effect=publish),
    )
    generator = SimpleNamespace(
        generate=AsyncMock(
            return_value={"html": "film source", "story": "One action", "narration": ""}
        )
    )
    sandboxes = SimpleNamespace(
        analyse_launch_music=AsyncMock(return_value={"onset_times": [0.2, 1.4]}),
        render_launch_video=AsyncMock(
            return_value={"content": b"checked-mp4", "report": {"passed": True, "warnings": []}}
        ),
    )
    studio = SimpleNamespace(
        voice=AsyncMock(return_value=SimpleNamespace(audio=b"voice", words=[{"word": "Action"}]))
    )

    def validator(raw):
        assert raw == b"checked-mp4"

    monkeypatch.setattr(module, "validate_launch_video", validator)
    activities = LaunchVideoActivities(
        database=db, storage=storage, generator=generator, sandboxes=sandboxes, studio=studio
    )
    return SimpleNamespace(**locals())


@pytest.mark.asyncio
async def test_build_reuses_saved_generation_and_publishes_exact_checked_bytes(rig):
    await rig.activities.build(str(rig.run.id))
    await rig.activities.build(str(rig.run.id))
    assert rig.generator.generate.await_count == 1
    assert rig.storage.publish_state_document.await_count == 1
    published = rig.storage.publish_state_document.call_args.kwargs
    assert published["path"] == "launches/one-action.mp4"
    assert published["content"] == b"checked-mp4"
    rendered = rig.sandboxes.render_launch_video.call_args.kwargs
    assert rendered["assets"] == {"product.png": b"png bytes"}
    assert rendered["music"] == b"approved music bytes"
    assert rendered["voice"] is None and rendered["captions"] is None
    assert await rig.activities.launch_review(str(rig.run.id)) is True
    await rig.activities.launch_approval(str(rig.run.id))
    await rig.activities.launch_project(str(rig.run.id))
    assert rig.run.approved
    assert rig.run.success["artifact_path"] == published["path"]


@pytest.mark.asyncio
async def test_failed_source_check_repairs_once_and_preserves_previous_draft(rig):
    rig.sandboxes.render_launch_video.side_effect = [
        {"content": b"", "report": {"passed": False, "issues": ["overlapping_text"]}},
        {"content": b"checked-mp4", "report": {"passed": True}},
    ]
    await rig.activities.build(str(rig.run.id))
    assert rig.generator.generate.await_count == 2
    second = rig.generator.generate.call_args.kwargs
    assert second["problems"] == ["overlapping_text"]
    assert second["previous"]["html"] == "film source"
    assert rig.db.receipts[f"{rig.run.id}:launch:commit"].result["repairs"] == 1


@pytest.mark.asyncio
async def test_stops_after_two_repairs_without_publishing(rig):
    rig.sandboxes.render_launch_video.return_value = {
        "content": b"",
        "report": {"passed": False, "issues": ["unreadable_text"]},
    }
    with pytest.raises(LaunchVideoError, match="two repairs"):
        await rig.activities.build(str(rig.run.id))
    assert rig.generator.generate.await_count == 3
    rig.storage.publish_state_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_sandbox_failure_replay_does_not_repurchase_the_model(rig):
    rig.sandboxes.render_launch_video.side_effect = RuntimeError("service unavailable")
    for _ in range(2):
        with pytest.raises(RuntimeError, match="service unavailable"):
            await rig.activities.build(str(rig.run.id))
    assert rig.generator.generate.await_count == 1
    assert rig.sandboxes.render_launch_video.await_count == 2


@pytest.mark.asyncio
async def test_replay_keeps_failed_checks_and_the_pinned_context(rig):
    rig.sandboxes.render_launch_video.side_effect = [
        {"content": b"", "report": {"passed": False, "issues": ["overlap"]}},
        RuntimeError("render service unavailable"),
        {"content": b"checked-mp4", "report": {"passed": True}},
    ]
    with pytest.raises(RuntimeError, match="service unavailable"):
        await rig.activities.build(str(rig.run.id))
    await rig.activities.build(str(rig.run.id))
    assert rig.generator.generate.await_count == 2
    assert rig.sandboxes.render_launch_video.await_count == 3
    assert rig.storage.head_sha.await_count == 1
    assert [
        call.kwargs["attempt"] for call in rig.sandboxes.render_launch_video.call_args_list
    ] == [
        0,
        1,
        1,
    ]


@pytest.mark.asyncio
async def test_missing_studio_image_is_checked_even_for_silent_films_before_model(rig):
    rig.run.input.update(sound_mode="silent")
    rig.sandboxes.analyse_launch_music.side_effect = RuntimeError("rebuild Studio image")
    with pytest.raises(RuntimeError, match="rebuild Studio image"):
        await rig.activities.build(str(rig.run.id))
    assert rig.sandboxes.analyse_launch_music.call_args.kwargs["music"] is None
    rig.generator.generate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["started", "failed"])
async def test_unknown_model_attempt_cannot_be_purchased_again(rig, status):
    key = f"{rig.run.id}:launch:model:0"
    rig.db.receipts[key] = EffectReceipt(key, "launch_model", status, None)
    with pytest.raises(LaunchVideoError, match="not purchased again"):
        await rig.activities.build(str(rig.run.id))
    rig.generator.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_failure_is_retained_as_unconfirmed_and_not_retried(rig):
    rig.generator.generate.side_effect = RuntimeError("private provider detail")
    with pytest.raises(LaunchVideoError, match="request did not complete"):
        await rig.activities.build(str(rig.run.id))
    with pytest.raises(LaunchVideoError, match="not purchased again"):
        await rig.activities.build(str(rig.run.id))
    assert rig.generator.generate.await_count == 1


@pytest.mark.asyncio
async def test_confirmed_invalid_model_output_gets_a_separate_bounded_repair(rig):
    draft = rig.generator.generate.return_value
    rig.generator.generate.side_effect = [LaunchVideoOutputError("Incomplete film result"), draft]
    await rig.activities.build(str(rig.run.id))
    assert rig.generator.generate.await_count == 2
    repair = rig.generator.generate.call_args.kwargs
    assert repair["previous"] is None
    assert repair["problems"] == ["Incomplete film result"]
    assert rig.db.receipts[f"{rig.run.id}:launch:model:0"].result == {
        "invalid_output": True,
        "problems": ["Incomplete film result"],
    }


@pytest.mark.asyncio
async def test_three_confirmed_invalid_outputs_stop_without_renders_or_publication(rig):
    rig.generator.generate.side_effect = LaunchVideoOutputError("Incomplete film result")
    with pytest.raises(LaunchVideoError, match="two repairs"):
        await rig.activities.build(str(rig.run.id))
    assert rig.generator.generate.await_count == 3
    rig.sandboxes.render_launch_video.assert_not_awaited()
    rig.storage.publish_state_document.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("captions", [True, False])
async def test_narration_uses_server_studio_and_preserves_caption_choice(rig, captions):
    rig.run.input.update(
        sound_mode="voice_and_music", captions=captions, language="Turkish", stt_language="tr"
    )
    rig.generator.generate.return_value["narration"] = "One product action"
    await rig.activities.build(str(rig.run.id))
    spoken = rig.studio.voice.call_args.kwargs
    assert spoken["request_id"].isalnum() and len(spoken["request_id"]) == 64
    assert spoken["text"] == "One product action"
    assert spoken["language_code"] == "Turkish"
    assert spoken["transcription_language"] == "tr"
    rendered = rig.sandboxes.render_launch_video.call_args.kwargs
    assert rendered["voice"] == b"voice"
    assert rendered["captions"] == ([{"word": "Action"}] if captions else None)


@pytest.mark.asyncio
async def test_html_repair_reuses_the_same_paid_narration_request(rig):
    rig.run.input.update(sound_mode="voice_and_music", captions=False)
    rig.generator.generate.return_value["narration"] = "One product action"
    rig.sandboxes.render_launch_video.side_effect = [
        {"content": b"", "report": {"passed": False, "issues": ["overlap"]}},
        {"content": b"checked-mp4", "report": {"passed": True}},
    ]
    await rig.activities.build(str(rig.run.id))
    calls = rig.studio.voice.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs["request_id"] == calls[1].kwargs["request_id"]


@pytest.mark.asyncio
async def test_changed_narration_gets_a_distinct_voice_request(rig):
    rig.run.input.update(sound_mode="voice_and_music", captions=False)
    first = {**rig.generator.generate.return_value, "narration": "One product action"}
    second = {**first, "narration": "A different product action"}
    rig.generator.generate.side_effect = [first, second]
    rig.sandboxes.render_launch_video.side_effect = [
        {"content": b"", "report": {"passed": False, "issues": ["overlap"]}},
        {"content": b"checked-mp4", "report": {"passed": True}},
    ]
    await rig.activities.build(str(rig.run.id))
    calls = rig.studio.voice.call_args_list
    assert calls[0].kwargs["request_id"] != calls[1].kwargs["request_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"asset_paths": [".env.png"]},
        {"asset_paths": ["../private.png"]},
        {"asset_paths": ["a/private.svg"]},
        {"asset_paths": ["a/same.png", "b/same.png"]},
        {"music_path": "config/secret.env"},
        {"music_license": ""},
    ],
)
async def test_refuses_unapproved_or_unsafe_assets_before_model_purchase(rig, change):
    rig.run.input.update(change)
    with pytest.raises(LaunchVideoError):
        await rig.activities.build(str(rig.run.id))
    rig.generator.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_refuses_a_changed_pinned_model_route(rig):
    rig.storage.read_canonical_artifact.return_value = b"{}"
    rig.storage.read_canonical_artifact.side_effect = None
    with pytest.raises(LaunchVideoError, match="pinned launch model"):
        await rig.activities.build(str(rig.run.id))
    rig.generator.generate.assert_not_awaited()


def test_native_workflow_has_explicit_registration_and_safe_output_name():
    assert registered_workflow_implementations()[KEY] is LaunchVideoWorkflow
    assert launch_path("feature-one") == "launches/feature-one.mp4"
    with pytest.raises(LaunchVideoError):
        launch_path("../private")
