"""Bounded metadata validation for project-owned X attachments; no transcoding or URL fetches."""

from __future__ import annotations

import struct
from pathlib import PurePosixPath

from tin_lite.project_files import safe_project_file_path

MAX_IMAGE_BYTES = 5_000_000
MAX_VIDEO_BYTES = 64_000_000
MAX_DIMENSION = 8192
MAX_PIXELS = 40_000_000
MAX_VIDEO_SECONDS = 300
MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".mp4": "video/mp4",
}


def _dimensions(width: int, height: int) -> None:
    if not 1 <= width <= MAX_DIMENSION or not 1 <= height <= MAX_DIMENSION:
        raise ValueError("Media dimensions must be between 1 and 8192 pixels.")
    if width * height > MAX_PIXELS:
        raise ValueError("Choose an image or video with at most 40 megapixels.")


def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        raise ValueError("The file is not a complete JPEG image.")
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 255:
            break
        while offset < len(data) and data[offset] == 255:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in {0xDA, 0xD9}:
            break
        if marker in {0x01, *range(0xD0, 0xD8)}:
            continue
        if offset + 2 > len(data):
            break
        size = int.from_bytes(data[offset : offset + 2], "big")
        if size < 2 or offset + size > len(data):
            break
        if marker in {0xC0, 0xC1, 0xC2} and size >= 8:
            return struct.unpack(
                ">HH", data[offset + 5 : offset + 7] + data[offset + 3 : offset + 5]
            )
        offset += size
    raise ValueError("JPEG dimensions could not be read; export a standard JPEG or PNG.")


def _boxes(data: bytes, start: int, end: int):
    count = 0
    while start < end:
        count += 1
        if count > 10_000 or start + 8 > end:
            raise ValueError("Invalid or excessively complex MP4 structure.")
        size, kind = struct.unpack_from(">I4s", data, start)
        header = 8
        if size == 1:
            if start + 16 > end:
                raise ValueError("Incomplete MP4 box.")
            size = struct.unpack_from(">Q", data, start + 8)[0]
            header = 16
        elif size == 0:
            size = end - start
        if size < header or start + size > end:
            raise ValueError("Incomplete MP4 box.")
        yield kind, start + header, start + size
        start += size


def _video_metadata(data: bytes) -> dict:
    top = list(_boxes(data, 0, len(data)))
    if not any(k == b"ftyp" for k, _, _ in top) or not any(k == b"mdat" for k, _, _ in top):
        raise ValueError("Choose a complete MP4 video.")
    duration = None
    dimensions = []
    codecs = []

    def visit(start, end, depth=0):
        nonlocal duration
        if depth > 8:
            raise ValueError("MP4 nesting is too deep.")
        for kind, begin, finish in _boxes(data, start, end):
            body = data[begin:finish] if kind in {b"mvhd", b"tkhd"} else b""
            if kind in {b"moov", b"trak", b"mdia", b"minf", b"stbl"}:
                visit(begin, finish, depth + 1)
            elif kind == b"mvhd":
                if len(body) < 20 or body[0] not in {0, 1}:
                    raise ValueError("MP4 duration is unavailable.")
                offset = 20 if body[0] else 12
                if len(body) < offset + (12 if body[0] else 8):
                    raise ValueError("MP4 duration is incomplete.")
                scale = int.from_bytes(body[offset : offset + 4], "big")
                ticks = int.from_bytes(body[offset + 4 : offset + (12 if body[0] else 8)], "big")
                if not scale:
                    raise ValueError("MP4 duration is invalid.")
                duration = ticks / scale
            elif kind == b"tkhd" and len(body) >= 84:
                w, h = struct.unpack(">II", body[-8:])
                if w and h:
                    dimensions.append((w >> 16, h >> 16))
            elif kind == b"stsd":
                if finish - begin < 8:
                    raise ValueError("MP4 codec information is incomplete.")
                for codec, _, _ in _boxes(data, begin + 8, finish):
                    codecs.append(codec)

    for kind, start, end in top:
        if kind == b"moov":
            visit(start, end)
    if duration is None or not 0 < duration <= MAX_VIDEO_SECONDS:
        raise ValueError("Choose an MP4 demo no longer than five minutes.")
    if not dimensions or not any(c in {b"avc1", b"avc3"} for c in codecs):
        raise ValueError("Export the video as MP4 with H.264 video and optional AAC audio.")
    if any(c not in {b"avc1", b"avc3", b"mp4a"} for c in codecs):
        raise ValueError("Export the video as MP4 with H.264 video and optional AAC audio.")
    for width, height in dimensions:
        _dimensions(width, height)
    return {"width": dimensions[0][0], "height": dimensions[0][1], "duration_seconds": duration}


def validate_media(path: str, content: bytes, media_type: str | None = None) -> dict:
    if not safe_project_file_path(path):
        raise ValueError("Choose an ordinary project file path.")
    expected = MEDIA_TYPES.get(PurePosixPath(path).suffix.lower())
    if not expected or (media_type is not None and media_type != expected):
        raise ValueError("Choose a JPEG, PNG or MP4 file with its matching content type.")
    limit = MAX_VIDEO_BYTES if expected == "video/mp4" else MAX_IMAGE_BYTES
    if not content or len(content) > limit:
        raise ValueError(f"Choose a nonempty file no larger than {limit // 1_000_000} MB.")
    if expected == "video/mp4":
        metadata = _video_metadata(content)
    else:
        if expected == "image/png":
            if (
                not content.startswith(b"\x89PNG\r\n\x1a\n")
                or len(content) < 45
                or content[8:16] != b"\x00\x00\x00\rIHDR"
                or content[-12:] != b"\x00\x00\x00\x00IEND\xaeB`\x82"
            ):
                raise ValueError("The file is not a complete PNG image.")
            width, height = struct.unpack(">II", content[16:24])
        else:
            width, height = _jpeg_dimensions(content)
        _dimensions(width, height)
        metadata = {"width": width, "height": height}
    return {"media_type": expected, "bytes": len(content), **metadata}
