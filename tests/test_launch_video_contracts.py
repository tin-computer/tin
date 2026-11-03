from __future__ import annotations

import struct

import pytest

from tin_lite.studio_contracts import (
    DEMO_VIDEO_VALIDATOR,
    LAUNCH_VIDEO_DIMENSIONS,
    MAX_LAUNCH_VIDEO_BYTES,
    validate_demo_video,
    validate_launch_video,
)


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def _sample_description(codec: bytes, payload: bytes) -> bytes:
    return _box(b"stsd", bytes(4) + struct.pack(">I", 1) + _box(codec, payload))


def _mp4(
    *,
    width: int = 1920,
    height: int = 1080,
    seconds: float = 20,
    timescale: int = 1000,
    video_tracks: int = 1,
    audio_tracks: int = 1,
    faststart: bool = True,
    movie_header: bytes | None = None,
    track_header: bytes | None = None,
    handler_header: bytes | None = None,
    moov_tail: bytes = b"",
    track_seconds: dict[bytes, float] | None = None,
    media_boxes: dict[bytes, bytes] | None = None,
    sample_boxes: dict[bytes, bytes] | None = None,
    video_codec: bytes = b"avc1",
    audio_codec: bytes = b"mp4a",
    encoded_size: tuple[int, int] | None = None,
) -> bytes:
    mvhd = _box(
        b"mvhd",
        movie_header
        if movie_header is not None
        else bytes(12) + struct.pack(">II", timescale, int(seconds * timescale)) + bytes(80),
    )

    def trak(handler: bytes) -> bytes:
        tkhd = _box(
            b"tkhd",
            track_header
            if track_header is not None
            else bytes(76) + struct.pack(">II", width << 16, height << 16),
        )
        hdlr = _box(
            b"hdlr",
            handler_header if handler_header is not None else bytes(8) + handler + bytes(12),
        )
        mdhd = _box(
            b"mdhd",
            bytes(12)
            + struct.pack(
                ">II", timescale, int((track_seconds or {}).get(handler, seconds) * timescale)
            )
            + bytes(4),
        )
        encoded_width, encoded_height = encoded_size or (width, height)
        description = _sample_description(
            video_codec if handler == b"vide" else audio_codec,
            bytes(24) + struct.pack(">HH", encoded_width, encoded_height) + bytes(50)
            if handler == b"vide"
            else bytes(28),
        )
        stbl = _box(b"stbl", (sample_boxes or {}).get(handler, description))
        mdia = _box(b"mdia", (media_boxes or {}).get(handler, mdhd) + hdlr + _box(b"minf", stbl))
        return _box(b"trak", tkhd + mdia)

    moov = _box(
        b"moov", mvhd + trak(b"vide") * video_tracks + trak(b"soun") * audio_tracks + moov_tail
    )
    mdat = _box(b"mdat", bytes(64))
    ftyp = _box(b"ftyp", b"isom" + bytes(4) + b"isomiso2mp41")
    return ftyp + (moov + mdat if faststart else mdat + moov)


@pytest.mark.parametrize(("width", "height"), LAUNCH_VIDEO_DIMENSIONS)
def test_launch_video_accepts_all_three_shapes(width, height) -> None:
    content = _mp4(width=width, height=height)
    result = validate_launch_video(content)
    assert (result.width, result.height, result.duration_seconds) == (width, height, 20)
    assert result.has_audio


@pytest.mark.parametrize("seconds", [8, 90])
def test_launch_video_accepts_duration_boundaries(seconds) -> None:
    assert validate_launch_video(_mp4(seconds=seconds)).duration_seconds == seconds


@pytest.mark.parametrize("seconds", [0, 7.999, 90.001])
def test_launch_video_rejects_duration_outside_its_window(seconds) -> None:
    with pytest.raises(ValueError, match="launch video must run 8-90 seconds"):
        validate_launch_video(_mp4(seconds=seconds))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"width": 1280, "height": 720}, "1920x1080"),
        ({"faststart": False}, "faststart"),
        ({"audio_tracks": 0}, "voice track"),
        ({"video_tracks": 0}, "no video track"),
        ({"video_tracks": 2}, "exactly one video track"),
        ({"audio_tracks": 2}, "exactly one audio track"),
        ({"timescale": 0}, "invalid timescale"),
        ({"movie_header": b""}, "truncated movie header"),
        ({"movie_header": bytes(12)}, "invalid movie header"),
        ({"movie_header": b"\x01" + bytes(30)}, "invalid movie header"),
        ({"movie_header": b"\x02" + bytes(99)}, "invalid movie header"),
        ({"track_header": b""}, "invalid video track header"),
        ({"track_header": bytes(83)}, "invalid video track header"),
        ({"track_header": b"\x01" + bytes(94)}, "invalid video track header"),
        ({"handler_header": bytes(11)}, "invalid handler"),
        ({"moov_tail": b"x"}, "truncated box header"),
        ({"moov_tail": _box(b"mvhd", bytes(100))}, "duplicate mvhd"),
    ],
)
def test_launch_video_rejects_invalid_shape_layout_and_headers(changes, message) -> None:
    with pytest.raises(ValueError, match=message):
        validate_launch_video(_mp4(**changes))


def test_launch_video_accepts_version_one_timing_and_track_headers() -> None:
    movie_header = b"\x01" + bytes(19) + struct.pack(">IQ", 1000, 20_000) + bytes(80)
    track_header = b"\x01" + bytes(87) + struct.pack(">II", 1920 << 16, 1080 << 16)
    assert (
        validate_launch_video(
            _mp4(movie_header=movie_header, track_header=track_header)
        ).duration_seconds
        == 20
    )


@pytest.mark.parametrize("handler", [b"vide", b"soun"])
@pytest.mark.parametrize("seconds", [8, 19.9, 20.1, 32])
def test_launch_rejects_tracks_that_do_not_cover_the_movie(handler, seconds) -> None:
    with pytest.raises(ValueError, match="track duration"):
        validate_launch_video(_mp4(track_seconds={handler: seconds}))


@pytest.mark.parametrize("handler", [b"vide", b"soun"])
def test_launch_allows_small_media_timing_padding(handler) -> None:
    assert validate_launch_video(_mp4(track_seconds={handler: 20.021})).duration_seconds == 20


@pytest.mark.parametrize(
    "media_boxes",
    [
        b"",
        _box(b"mdhd", b""),
        _box(b"mdhd", bytes(19)),
        _box(b"mdhd", b"\x01" + bytes(30)),
        _box(b"mdhd", b"\x02" + bytes(35)),
        _box(b"mdhd", bytes(12) + struct.pack(">II", 0, 20_000) + bytes(4)),
        _box(b"mdhd", bytes(12) + struct.pack(">II", 1000, 20_000) + bytes(4)) * 2,
    ],
)
def test_launch_rejects_missing_or_invalid_track_timing(media_boxes) -> None:
    with pytest.raises(ValueError, match="media header|duplicate mdhd"):
        validate_launch_video(_mp4(media_boxes={b"vide": media_boxes}))


def test_launch_accepts_version_one_media_timing() -> None:
    mdhd = _box(b"mdhd", b"\x01" + bytes(19) + struct.pack(">IQ", 1000, 20_000) + bytes(4))
    assert validate_launch_video(_mp4(media_boxes={b"vide": mdhd, b"soun": mdhd})).has_audio


@pytest.mark.parametrize("video_codec", [b"avc1", b"avc3"])
def test_launch_accepts_h264_sample_entries(video_codec) -> None:
    assert validate_launch_video(_mp4(video_codec=video_codec)).width == 1920


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"video_codec": b"vp09"}, "H.264"),
        ({"audio_codec": b"ac-3"}, "AAC"),
        ({"encoded_size": (1280, 720)}, "encoded dimensions"),
    ],
)
def test_launch_rejects_other_codecs_or_disagreeing_encoded_shape(changes, message) -> None:
    with pytest.raises(ValueError, match=message):
        validate_launch_video(_mp4(**changes))


@pytest.mark.parametrize(
    ("handler", "sample_boxes"),
    [
        (b"vide", b""),
        (b"vide", _box(b"stsd", b"")),
        (b"vide", _box(b"stsd", bytes(4) + struct.pack(">I", 0))),
        (b"vide", _box(b"stsd", bytes(4) + struct.pack(">I", 1))),
        (
            b"vide",
            _box(b"stsd", b"\x01" + bytes(3) + struct.pack(">I", 1) + _box(b"avc1", bytes(78))),
        ),
        (b"vide", _sample_description(b"avc1", bytes(77))),
        (b"soun", _sample_description(b"mp4a", bytes(27))),
        (b"vide", _sample_description(b"avc1", bytes(78)) * 2),
        (
            b"vide",
            _box(b"stsd", bytes(4) + struct.pack(">I", 2) + _box(b"avc1", bytes(78)) * 2),
        ),
        (
            b"vide",
            _box(b"stsd", bytes(4) + struct.pack(">I", 1) + _box(b"avc1", bytes(78)) * 2),
        ),
    ],
)
def test_launch_rejects_missing_truncated_or_duplicate_sample_descriptions(handler, sample_boxes):
    with pytest.raises(ValueError, match="sample description|duplicate stsd"):
        validate_launch_video(_mp4(sample_boxes={handler: sample_boxes}))


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"", "must contain"),
        (bytes(100), "ftyp"),
        (_mp4()[:-1], "invalid box layout"),
        (_mp4() + b"x", "truncated box header"),
        (_mp4() + struct.pack(">I", 1) + b"free", "truncated large box"),
        (_mp4() + _box(b"moov", bytes(64)), "duplicate moov"),
        (_mp4(movie_header=bytes(12) + struct.pack(">II", 1, 0xFFFFFFFF)), "8-90"),
    ],
)
def test_launch_video_rejects_truncated_or_invalid_mp4(content, message) -> None:
    with pytest.raises(ValueError, match=message):
        validate_launch_video(content)


def test_launch_video_size_limit() -> None:
    content = _mp4()
    at_cap = content + _box(b"free", bytes(MAX_LAUNCH_VIDEO_BYTES - len(content) - 8))
    validate_launch_video(at_cap)
    with pytest.raises(ValueError, match="launch video must contain 1-16000000 bytes"):
        validate_launch_video(at_cap + b"x")


def test_launch_shapes_do_not_widen_the_existing_demo_contract() -> None:
    assert DEMO_VIDEO_VALIDATOR == "demo-video.v1"
    validate_demo_video(_mp4(width=1080, height=1920))
    for width, height in ((1920, 1080), (1080, 1080)):
        with pytest.raises(ValueError, match="demo video must be 1080x1920 portrait"):
            validate_demo_video(_mp4(width=width, height=height))
