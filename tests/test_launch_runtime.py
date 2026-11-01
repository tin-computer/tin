"""No network or provider calls: CLI boundaries and real local media checks."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).parents[1]
NONUNIFORM_FRAME = bytes([0, 0, 0]) * (64 * 18) + bytes([255, 255, 255]) * (64 * 18)


@pytest.fixture
def toolkit(monkeypatch):
    folder = ROOT / "sandbox/studio"
    monkeypatch.syspath_prepend(str(folder))
    spec = importlib.util.spec_from_file_location("launch_toolkit", folder / "tin_launch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "7", "91"])
def test_duration_rejects_unbounded_or_unsupported_values(toolkit, value):
    with pytest.raises(argparse.ArgumentTypeError, match="8–90 seconds"):
        toolkit.seconds(value)


@pytest.mark.parametrize(
    "mode,script,prefix",
    [
        ("render", "render.mjs", ["video"]),
        ("stills", "render.mjs", ["stills"]),
        ("review", "review_frames.mjs", []),
    ],
)
def test_cli_dispatch_preserves_format_and_exit_status(toolkit, monkeypatch, mode, script, prefix):
    call = Mock(return_value=1)
    monkeypatch.setattr(toolkit.subprocess, "call", call)
    args = ["--page", "film/index.html", "--w", "1080", "--h", "1920"]
    assert toolkit.main([mode, *args]) == 1
    assert call.call_args.args[0] == ["node", str(toolkit.HERE / "launch" / script), *prefix, *args]


@pytest.mark.parametrize("format", ["landscape", "portrait", "square"])
def test_check_validates_requested_shape_and_duration_before_decode(
    toolkit, monkeypatch, tmp_path, format
):
    path = tmp_path / "out.mp4"
    path.write_bytes(b"not used by patched parser")
    width, height = toolkit.DIMENSIONS[format]
    monkeypatch.setattr(
        toolkit,
        "validate_launch_video",
        lambda _: SimpleNamespace(width=width, height=height, duration_seconds=24),
    )
    monkeypatch.setattr(
        toolkit,
        "probe",
        lambda _: {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": width,
                    "height": height,
                    "duration": "24",
                },
                {"codec_type": "audio", "codec_name": "aac", "duration": "24"},
            ]
        },
    )
    run = Mock()
    monkeypatch.setattr(toolkit.subprocess, "run", run)
    monkeypatch.setattr(toolkit.subprocess, "check_output", Mock(return_value=NONUNIFORM_FRAME))
    toolkit.check(SimpleNamespace(path=path, format=format, duration=24))
    assert "-xerror" in run.call_args.args[0]
    with pytest.raises(ValueError, match="agreed film duration"):
        toolkit.check(SimpleNamespace(path=path, format=format, duration=25))
    other = "portrait" if format == "landscape" else "landscape"
    with pytest.raises(ValueError, match="agreed film format"):
        toolkit.check(SimpleNamespace(path=path, format=other, duration=24))
    assert run.call_count == 1


def test_check_refuses_wrong_codec(toolkit, monkeypatch, tmp_path):
    path = tmp_path / "out.mp4"
    path.write_bytes(b"test")
    monkeypatch.setattr(
        toolkit,
        "validate_launch_video",
        lambda _: SimpleNamespace(width=1920, height=1080, duration_seconds=20),
    )
    monkeypatch.setattr(
        toolkit,
        "probe",
        lambda _: {
            "streams": [
                {"codec_type": "video", "codec_name": "vp9"},
                {"codec_type": "audio", "codec_name": "aac"},
            ]
        },
    )
    with pytest.raises(ValueError, match="H.264"):
        toolkit.check(SimpleNamespace(path=path, format="landscape", duration=20))


def test_mix_never_silently_drops_requested_music(toolkit, monkeypatch, tmp_path):
    monkeypatch.setattr(toolkit, "probe", lambda _: {"format": {"duration": "20"}})
    args = SimpleNamespace(
        video=tmp_path / "silent.mp4",
        music=None,
        effects=None,
        voice=None,
        silent=False,
        out=tmp_path / "out.mp4",
    )
    with pytest.raises(ValueError, match="choose music"):
        toolkit.mix(args)


@pytest.mark.parametrize(
    "change,message",
    [
        ({"video_duration": "8"}, "video stream"),
        ({"audio_duration": "8"}, "audio stream"),
        ({"video_duration": "nan"}, "video stream"),
        ({"width": 1280, "height": 720}, "encoded picture"),
    ],
)
def test_check_rejects_streams_that_container_metadata_hides(
    toolkit, monkeypatch, tmp_path, change, message
):
    path = tmp_path / "out.mp4"
    path.write_bytes(b"patched container")
    monkeypatch.setattr(
        toolkit,
        "validate_launch_video",
        lambda _: SimpleNamespace(width=1920, height=1080, duration_seconds=20),
    )
    streams = [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "width": change.get("width", 1920),
            "height": change.get("height", 1080),
            "duration": change.get("video_duration", "20"),
        },
        {
            "codec_type": "audio",
            "codec_name": "aac",
            "duration": change.get("audio_duration", "20"),
        },
    ]
    monkeypatch.setattr(toolkit, "probe", lambda _: {"streams": streams})
    run = Mock()
    monkeypatch.setattr(toolkit.subprocess, "run", run)
    with pytest.raises(ValueError, match=message):
        toolkit.check(SimpleNamespace(path=path, format="landscape", duration=20))
    run.assert_not_called()


@pytest.mark.parametrize("color", [(0, 0, 0), (255, 255, 255), (180, 20, 120)])
def test_picture_rejects_uniform_black_white_or_color(toolkit, monkeypatch, color):
    raw = bytes(color) * (64 * 36 * 2)
    monkeypatch.setattr(toolkit.subprocess, "check_output", Mock(return_value=raw))
    with pytest.raises(ValueError, match="uniform blank"):
        toolkit.check_picture(Path("film.mp4"), 8)


def test_picture_rejects_only_flat_frames_even_when_their_colors_change(toolkit, monkeypatch):
    raw = bytes([0, 0, 0]) * (64 * 36) + bytes([250, 10, 0]) * (64 * 36)
    monkeypatch.setattr(toolkit.subprocess, "check_output", Mock(return_value=raw))
    with pytest.raises(ValueError, match="uniform blank"):
        toolkit.check_picture(Path("film.mp4"), 8)


@pytest.mark.parametrize("prefix", [b"", bytes([0, 0, 0]) * (64 * 36)])
def test_picture_accepts_static_nonuniform_imagery_and_blank_openings(toolkit, monkeypatch, prefix):
    sample = Mock(return_value=prefix + NONUNIFORM_FRAME * 2)
    monkeypatch.setattr(toolkit.subprocess, "check_output", sample)
    assert toolkit.check_picture(Path("film.mp4"), 90) == (3 if prefix else 2)
    command = sample.call_args.args[0]
    assert command[command.index("-vf") + 1] == "fps=2,scale=64:36"
    assert command[command.index("-frames:v") + 1] == "180"
    assert command[command.index("-t") + 1] == "90"
    assert sample.call_args.kwargs["timeout"] == 120


@pytest.mark.parametrize("raw", [b"", b"partial-frame"])
def test_picture_requires_complete_decoded_samples(toolkit, monkeypatch, raw):
    monkeypatch.setattr(toolkit.subprocess, "check_output", Mock(return_value=raw))
    with pytest.raises(ValueError, match="samples are missing or invalid"):
        toolkit.check_picture(Path("film.mp4"), 8)
