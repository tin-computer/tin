from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from e2b import CommandExitException, FileType

from tin_lite import e2b_runtime, studio_contracts
from tin_lite.e2b_runtime import E2BRuntime, _launch_caption_overlay

ROOT = "/home/user/launch"
RUN_ID = str(uuid4())


@pytest.fixture
def rig(monkeypatch):
    source = {
        "duration": 8,
        "source_width": 1920,
        "source_height": 1080,
        "errors": [],
        "warnings": [{"code": "manual_visual_surfaces"}],
        "frames_checked": 240,
    }
    files = {
        f"{ROOT}/review/report.json": json.dumps(source).encode(),
        f"{ROOT}/final.mp4": b"valid-mp4",
        f"{ROOT}/music-cut/cues.json": json.dumps(
            {"duration": 8, "onset_times": [0.2, 1.2], "limits": "not a verified beat grid"}
        ).encode(),
    }

    async def read(path, **_):
        return files[path]

    async def info(path, **_):
        return SimpleNamespace(type=FileType.FILE, symlink_target=None, size=len(files[path]))

    sandbox = SimpleNamespace(
        sandbox_id="launch-sandbox",
        commands=SimpleNamespace(
            run=AsyncMock(return_value=SimpleNamespace(stdout='{"full_decode":"passed"}'))
        ),
        files=SimpleNamespace(
            write=AsyncMock(),
            read=AsyncMock(side_effect=read),
            get_info=AsyncMock(side_effect=info),
        ),
        kill=AsyncMock(return_value=True),
    )
    create = AsyncMock(return_value=sandbox)
    monkeypatch.setattr(e2b_runtime.AsyncSandbox, "create", create)
    monkeypatch.setattr(
        studio_contracts,
        "validate_launch_video",
        lambda _: SimpleNamespace(width=1920, height=1080, duration_seconds=8),
    )
    runtime = E2BRuntime(
        api_key="control-plane-only",
        template="unused-basic",
        studio_api_template="studio-api-image",
        timeout_seconds=900,
        egress_allow_hosts=("some-provider",),
    )
    return SimpleNamespace(
        runtime=runtime, create=create, sandbox=sandbox, files=files, source=source
    )


async def render(rig, **changes):
    inputs = {
        "run_id": RUN_ID,
        "attempt": 0,
        "html": '<img src="assets/product.png"><script>window.seek=t=>{};</script>',
        "assets": {"product.png": b"image"},
        "duration_seconds": 8,
        "format": "landscape",
        "music": None,
        "voice": None,
    }
    return await rig.runtime.render_launch_video(**(inputs | changes))


@pytest.mark.asyncio
async def test_offline_render_passes_only_public_inputs_and_keeps_warnings(rig):
    result = await render(rig)
    assert result["content"] == b"valid-mp4"
    assert result["report"]["passed"]
    assert result["report"]["warnings"] == rig.source["warnings"]
    arguments = rig.create.call_args
    assert arguments.args == ("studio-api-image",)
    assert arguments.kwargs["timeout"] == 900
    network = arguments.kwargs["network"]
    assert set(network) == {"deny_out"}
    assert network["deny_out"](SimpleNamespace(all_traffic="all")) == ["all"]
    assert "envs" not in arguments.kwargs
    written = str(rig.sandbox.files.write.call_args_list)
    assert "control-plane-only" not in written
    assert ".env" not in written and "checkout" not in written
    rig.sandbox.files.write.assert_any_await(f"{ROOT}/film/assets/product.png", b"image")
    assert not any(
        call.args[0] == f"{ROOT}/film/product.png"
        for call in rig.sandbox.files.write.call_args_list
    )
    commands = [call.args[0] for call in rig.sandbox.commands.run.call_args_list]
    assert commands[0] == "tin-launch --help"
    assert any("--silent" in command for command in commands)
    assert any("--fps 30 --sub 1 --workers 4" in command for command in commands)
    rig.sandbox.kill.assert_awaited_once()


@pytest.mark.asyncio
async def test_silent_film_still_checks_image_readiness_before_model_generation(rig):
    cues = await rig.runtime.analyse_launch_music(run_id=RUN_ID, music=None, duration_seconds=8)
    assert cues == {}
    assert rig.sandbox.commands.run.call_args_list[0].args == ("tin-launch --help",)
    rig.sandbox.files.write.assert_not_awaited()
    rig.sandbox.kill.assert_awaited_once()


@pytest.mark.asyncio
async def test_music_uses_the_same_measured_cut_for_generation_and_mix(rig):
    cues = await rig.runtime.analyse_launch_music(run_id=RUN_ID, music=b"music", duration_seconds=8)
    assert cues["onset_times"] == [0.2, 1.2]
    assert "not a verified beat" in cues["limits"]
    result = await render(rig, music=b"music", voice=b"voice")
    assert result["report"]["passed"]
    commands = [call.args[0] for call in rig.sandbox.commands.run.call_args_list]
    analyses = [command for command in commands if command.startswith("tin-launch analyse")]
    assert len(analyses) == 2
    assert all(
        "--duration 8 --outdir /home/user/launch/music-cut" in command for command in analyses
    )
    assert any("--music /home/user/launch/music-cut/music.wav" in command for command in commands)
    assert any("--voice /home/user/launch/voice.input" in command for command in commands)
    assert rig.sandbox.kill.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"run_id": "not-a-uuid"},
        {"attempt": 3},
        {"attempt": True},
        {"duration_seconds": True},
        {"duration_seconds": 7},
        {"format": "landscape;curl attacker"},
        {"html": "x" * 120_001},
        {"assets": {"../.env": b"no"}},
        {"assets": {"evil.js": b"no"}},
        {"assets": {"photo.png": b"x" * 20_000_001}},
        {"music": b"x" * 20_000_001},
        {"voice": b"voice"},
        {"music": b"music", "voice": b"voice", "captions": []},
    ],
)
async def test_invalid_packet_fails_before_sandbox_purchase(rig, changes):
    with pytest.raises(ValueError):
        await render(rig, **changes)
    rig.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_error_returns_repair_evidence_without_render_or_publication(rig):
    rig.source["errors"] = [{"code": "text_clipped_by_container"}]
    rig.files[f"{ROOT}/review/report.json"] = json.dumps(rig.source).encode()
    result = await render(rig)
    assert result["content"] == b""
    assert not result["report"]["passed"]
    assert result["report"]["issues"] == ["text_clipped_by_container"]
    assert not any(
        call.args[0].startswith("tin-launch render")
        for call in rig.sandbox.commands.run.call_args_list
    )
    rig.sandbox.kill.assert_awaited_once()


@pytest.mark.asyncio
async def test_agreed_shape_is_checked_before_render(rig):
    result = await render(rig, format="portrait")
    assert not result["report"]["passed"]
    assert "dimensions" in result["report"]["issues"][0]
    rig.sandbox.kill.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_render_is_safe_repair_result_and_still_cleans_up(rig):
    async def run(command, **_):
        if command.startswith("tin-launch render"):
            raise CommandExitException("untrusted private script output", "", 1, None)
        return SimpleNamespace(stdout="{}")

    rig.sandbox.commands.run.side_effect = run
    result = await render(rig)
    assert not result["report"]["passed"] and not result["content"]
    assert "private script output" not in json.dumps(result["report"])
    rig.sandbox.kill.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_image_runtime_is_not_reported_as_a_model_defect(rig):
    rig.sandbox.commands.run.side_effect = CommandExitException(
        "tin-launch not found", "", 127, None
    )
    with pytest.raises(CommandExitException):
        await render(rig)
    rig.sandbox.kill.assert_awaited_once()


@pytest.mark.asyncio
async def test_timeout_cleans_up_without_requesting_a_model_repair(rig):
    rig.sandbox.commands.run.side_effect = TimeoutError("sandbox deadline")
    with pytest.raises(TimeoutError):
        await render(rig)
    rig.sandbox.kill.assert_awaited_once()


@pytest.mark.asyncio
async def test_delivery_rejects_oversize_file_before_read(rig):
    info = SimpleNamespace(type=FileType.FILE, symlink_target=None, size=16_000_001)

    async def get_info(path, **_):
        if path.endswith("final.mp4"):
            return info
        return SimpleNamespace(type=FileType.FILE, symlink_target=None, size=len(rig.files[path]))

    rig.sandbox.files.get_info.side_effect = get_info
    result = await render(rig)
    assert not result["report"]["passed"] and not result["content"]
    assert all(
        not call.args[0].endswith("final.mp4") for call in rig.sandbox.files.read.call_args_list
    )
    rig.sandbox.kill.assert_awaited_once()


def test_caption_overlay_escapes_script_closing_text_and_uses_timed_words():
    html = _launch_caption_overlay([{"word": "</script><script>bad()", "start": 0, "end": 1}], 8)
    assert "</script><script>bad()" not in html
    assert "\\u003c/script>" in html
    assert "FilmCaptions.create" in html and "paint(t)" in html


@pytest.mark.parametrize(
    "words",
    [
        [None],
        [{"word": "a", "start": float("nan"), "end": 1}],
        [{"word": "a", "start": 1, "end": 9}],
        [{"word": "a", "start": 0, "end": 2}, {"word": "b", "start": 1, "end": 3}],
    ],
)
def test_caption_overlay_rejects_invalid_timings(words):
    with pytest.raises(ValueError):
        _launch_caption_overlay(words, 8)
