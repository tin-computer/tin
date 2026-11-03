"""Creative-studio output contracts shared by the switchboard and the sandbox toolkit.

This module has no Tin imports so the exact same file ships inside the studio sandbox image
(`/opt/tin-lite/studio/studio_contracts.py`) and in the switchboard package
(`src/tin_lite/studio_contracts.py`); a test keeps the two copies byte-identical. The sandbox
uses it to pre-check an artifact before it commits; the switchboard uses it as the authority.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

CHARACTER_SVG_MEDIA_TYPE = "image/svg+xml"
DEMO_VIDEO_MEDIA_TYPE = "video/mp4"
LAUNCH_VIDEO_MEDIA_TYPE = "video/mp4"
CHARACTER_SVG_VALIDATOR = "character-svg.v1"
DEMO_VIDEO_VALIDATOR = "demo-video.v1"
LAUNCH_VIDEO_VALIDATOR = "launch-video.v1"

SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
CHARACTER_VIEWBOX = "0 0 512 512"
# The renderer shows exactly one mouth group and one eye group per frame; the optional
# expression group is shown only during the demo's payoff step.
CHARACTER_MOUTH_IDS = ("mouth-closed", "mouth-mid", "mouth-open")
CHARACTER_EYE_IDS = ("eyes-open", "eyes-closed")
CHARACTER_REQUIRED_IDS = (*CHARACTER_MOUTH_IDS, *CHARACTER_EYE_IDS)
CHARACTER_OPTIONAL_IDS = ("expr-happy",)
CHARACTER_STATE_IDS = (*CHARACTER_REQUIRED_IDS, *CHARACTER_OPTIONAL_IDS)
MAX_CHARACTER_SVG_BYTES = 64_000
MAX_CHARACTER_ELEMENTS = 1_500
# Text renders differently in every SVG engine and needs fonts the renderer cannot promise,
# so a character is pure geometry. Anything that can load, run, or animate is rejected.
_FORBIDDEN_SVG_ELEMENTS = frozenset(
    {
        "script",
        "foreignObject",
        "image",
        "a",
        "text",
        "tspan",
        "textPath",
        "animate",
        "animateMotion",
        "animateTransform",
        "set",
        "iframe",
        "video",
        "audio",
        "font",
    }
)
_HREF_ATTRIBUTES = ("href", f"{{{XLINK_NS}}}href")
_STYLE_URL = re.compile(r"url\(\s*['\"]?\s*([^'\")\s]*)", re.IGNORECASE)

MAX_DEMO_VIDEO_BYTES = 16_000_000
DEMO_VIDEO_MIN_SECONDS = 8.0
DEMO_VIDEO_MAX_SECONDS = 90.0
DEMO_VIDEO_WIDTH = 1080
DEMO_VIDEO_HEIGHT = 1920
MAX_LAUNCH_VIDEO_BYTES = 16_000_000
LAUNCH_VIDEO_MIN_SECONDS = 8.0
LAUNCH_VIDEO_MAX_SECONDS = 90.0
LAUNCH_VIDEO_DIMENSIONS = ((1920, 1080), (1080, 1920), (1080, 1080))


@dataclass(frozen=True)
class CharacterSvg:
    viewbox: str
    element_count: int
    optional_states: tuple[str, ...]


@dataclass(frozen=True)
class DemoVideo:
    duration_seconds: float
    width: int
    height: int
    has_audio: bool


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _check_style_text(text: str, *, where: str) -> None:
    lowered = text.lower()
    if "@import" in lowered or "expression(" in lowered or "javascript:" in lowered:
        raise ValueError(f"character SVG {where} loads or runs external content")
    for match in _STYLE_URL.finditer(text):
        if not match.group(1).startswith("#"):
            raise ValueError(f"character SVG {where} references an external URL")


def parse_character_svg(content: bytes) -> tuple[ET.Element, CharacterSvg]:
    """Parse and check one character SVG; returns the root element and its summary."""
    if not content or len(content) > MAX_CHARACTER_SVG_BYTES:
        raise ValueError(f"character SVG must contain 1-{MAX_CHARACTER_SVG_BYTES} bytes")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("character SVG must be UTF-8") from exc
    head = text.lstrip()[:512].lower()
    if "<!doctype" in text.lower() or "<!entity" in text.lower():
        raise ValueError("character SVG must not declare a DOCTYPE or entities")
    if not head.startswith(("<svg", "<?xml")):
        raise ValueError("character SVG must start with an <svg> element")
    try:
        root = ET.fromstring(text)  # noqa: S314 - size-capped, DOCTYPE and entities rejected
    except ET.ParseError as exc:
        raise ValueError(f"character SVG is not well-formed XML: {exc}") from exc
    if root.tag != f"{{{SVG_NS}}}svg":
        raise ValueError("character SVG root must be an svg element in the SVG namespace")
    viewbox = " ".join((root.get("viewBox") or "").split())
    if viewbox != CHARACTER_VIEWBOX:
        raise ValueError(f'character SVG viewBox must be "{CHARACTER_VIEWBOX}"')
    ids: dict[str, str] = {}
    count = 0
    for element in root.iter():
        count += 1
        if count > MAX_CHARACTER_ELEMENTS:
            raise ValueError(f"character SVG has more than {MAX_CHARACTER_ELEMENTS} elements")
        name = _local(element.tag)
        if not element.tag.startswith(f"{{{SVG_NS}}}"):
            raise ValueError(f"character SVG contains a non-SVG element: {name}")
        if name in _FORBIDDEN_SVG_ELEMENTS:
            raise ValueError(f"character SVG must not contain <{name}>")
        for attribute, value in element.attrib.items():
            local = _local(attribute)
            if local.lower().startswith("on"):
                raise ValueError("character SVG must not contain event handler attributes")
            if attribute in _HREF_ATTRIBUTES and not value.startswith("#"):
                raise ValueError("character SVG links must stay inside the document")
            if local == "style" or "url(" in value.lower():
                _check_style_text(value, where=f"{local} attribute")
            if local == "id":
                if value in ids:
                    raise ValueError(f"character SVG declares id {value!r} more than once")
                ids[value] = name
        if name == "style":
            _check_style_text(element.text or "", where="style element")
    for required in CHARACTER_REQUIRED_IDS:
        if required not in ids:
            raise ValueError(f'character SVG is missing the <g id="{required}"> group')
    for state in CHARACTER_STATE_IDS:
        if state in ids and ids[state] != "g":
            raise ValueError(f'character SVG state "{state}" must be a <g> group')
    optional = tuple(state for state in CHARACTER_OPTIONAL_IDS if state in ids)
    return root, CharacterSvg(viewbox=viewbox, element_count=count, optional_states=optional)


def validate_character_svg(content: bytes) -> CharacterSvg:
    return parse_character_svg(content)[1]


def character_variant_svg(
    content: bytes,
    *,
    mouth: str = "mouth-closed",
    eyes: str = "eyes-open",
    expression: str | None = None,
) -> bytes:
    """Return the SVG with exactly one mouth, one eye state, and one optional expression shown."""
    if mouth not in CHARACTER_MOUTH_IDS:
        raise ValueError(f"unknown mouth state {mouth!r}")
    if eyes not in CHARACTER_EYE_IDS:
        raise ValueError(f"unknown eye state {eyes!r}")
    if expression is not None and expression not in CHARACTER_OPTIONAL_IDS:
        raise ValueError(f"unknown expression {expression!r}")
    root, _summary = parse_character_svg(content)
    shown = {mouth, eyes}
    if expression is not None:
        shown.add(expression)
    for element in root.iter(f"{{{SVG_NS}}}g"):
        state = element.get("id")
        if state in CHARACTER_STATE_IDS:
            if state in shown:
                element.attrib.pop("display", None)
                element.set("visibility", "visible")
            else:
                element.set("display", "none")
    ET.register_namespace("", SVG_NS)
    ET.register_namespace("xlink", XLINK_NS)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _iter_boxes(
    data: bytes, start: int, end: int, *, name: str = "demo video", strict: bool = False
):
    position = start
    while position + 8 <= end:
        size = int.from_bytes(data[position : position + 4], "big")
        kind = data[position + 4 : position + 8]
        header = 8
        if size == 1:
            if position + 16 > end:
                raise ValueError(f"{name} has a truncated large box")
            size = int.from_bytes(data[position + 8 : position + 16], "big")
            header = 16
        elif size == 0:
            size = end - position
        if size < header or position + size > end:
            raise ValueError(f"{name} has an invalid box layout")
        yield kind, position + header, position + size, position
        position += size
    if strict and position != end:
        raise ValueError(f"{name} has a truncated box header")


def _find_box(
    data: bytes,
    start: int,
    end: int,
    kind: bytes,
    *,
    name: str = "demo video",
    strict: bool = False,
) -> tuple[int, int] | None:
    result = None
    for found, payload_start, payload_end, _offset in _iter_boxes(
        data, start, end, name=name, strict=strict
    ):
        if found == kind:
            if not strict:
                return payload_start, payload_end
            if result is not None:
                raise ValueError(f"{name} has duplicate {kind.decode('ascii', 'replace')} boxes")
            result = payload_start, payload_end
    return result


def validate_demo_video(content: bytes) -> DemoVideo:
    """Check an MP4 structurally: fast-start layout, one portrait video track, one audio track,
    and a duration inside the short-form window. No decoder runs on the switchboard."""
    return _validate_video(
        content,
        name="demo video",
        max_bytes=MAX_DEMO_VIDEO_BYTES,
        min_seconds=DEMO_VIDEO_MIN_SECONDS,
        max_seconds=DEMO_VIDEO_MAX_SECONDS,
        dimensions=((DEMO_VIDEO_WIDTH, DEMO_VIDEO_HEIGHT),),
        dimension_error=f"demo video must be {DEMO_VIDEO_WIDTH}x{DEMO_VIDEO_HEIGHT} portrait",
    )


def validate_launch_video(content: bytes) -> DemoVideo:
    """Check fast-start MP4 metadata, duration, one video track and one audio track.

    Accept landscape, portrait or square Full HD. Check media timing and H.264/AAC sample
    descriptions without decoding media; the sandbox checks actual playback and encoding.
    """
    return _validate_video(
        content,
        name="launch video",
        max_bytes=MAX_LAUNCH_VIDEO_BYTES,
        min_seconds=LAUNCH_VIDEO_MIN_SECONDS,
        max_seconds=LAUNCH_VIDEO_MAX_SECONDS,
        dimensions=LAUNCH_VIDEO_DIMENSIONS,
        dimension_error="launch video must be 1920x1080, 1080x1920 or 1080x1080",
        strict=True,
    )


def _validate_launch_media(
    content: bytes, mdia: tuple[int, int], handler: bytes, movie_seconds: float
) -> tuple[int, int] | None:
    mdhd = _find_box(content, *mdia, b"mdhd", name="launch video", strict=True)
    if mdhd is None or mdhd[0] == mdhd[1]:
        raise ValueError("launch video track has no media header")
    version = content[mdhd[0]]
    if version not in (0, 1) or mdhd[1] - mdhd[0] < (36 if version == 1 else 24):
        raise ValueError("launch video track has an invalid media header")
    base = mdhd[0] + (20 if version == 1 else 12)
    timescale = int.from_bytes(content[base : base + 4], "big")
    duration = int.from_bytes(content[base + 4 : base + (12 if version == 1 else 8)], "big")
    if timescale <= 0:
        raise ValueError("launch video track has an invalid media header timescale")
    if abs(duration / timescale - movie_seconds) > 0.05:
        raise ValueError("launch video track duration differs from the movie duration")
    minf = _find_box(content, *mdia, b"minf", name="launch video", strict=True)
    stbl = (
        _find_box(content, *minf, b"stbl", name="launch video", strict=True)
        if minf is not None
        else None
    )
    stsd = (
        _find_box(content, *stbl, b"stsd", name="launch video", strict=True)
        if stbl is not None
        else None
    )
    if (
        stsd is None
        or stsd[1] - stsd[0] < 8
        or content[stsd[0] : stsd[0] + 4] != bytes(4)
        or int.from_bytes(content[stsd[0] + 4 : stsd[0] + 8], "big") != 1
    ):
        raise ValueError("launch video track must have one sample description")
    entries = _iter_boxes(content, stsd[0] + 8, stsd[1], name="launch video", strict=True)
    entry = next(entries, None)
    if entry is None or next(entries, None) is not None:
        raise ValueError("launch video track must have one sample description")
    kind, start, end, _offset = entry
    if handler == b"vide":
        if kind not in (b"avc1", b"avc3"):
            raise ValueError("launch video requires an H.264 sample description")
        if end - start < 78:
            raise ValueError("launch video has a truncated video sample description")
        return (
            int.from_bytes(content[start + 24 : start + 26], "big"),
            int.from_bytes(content[start + 26 : start + 28], "big"),
        )
    if kind != b"mp4a":
        raise ValueError("launch video requires an AAC sample description")
    if end - start < 28:
        raise ValueError("launch video has a truncated audio sample description")
    return None


def _validate_video(
    content: bytes,
    *,
    name: str,
    max_bytes: int,
    min_seconds: float,
    max_seconds: float,
    dimensions: tuple[tuple[int, int], ...],
    dimension_error: str,
    strict: bool = False,
) -> DemoVideo:
    if not content or len(content) > max_bytes:
        raise ValueError(f"{name} must contain 1-{max_bytes} bytes")
    if len(content) < 64 or content[4:8] != b"ftyp":
        raise ValueError(f"{name} must be an MP4 file starting with an ftyp box")
    offsets: dict[bytes, int] = {}
    spans: dict[bytes, tuple[int, int]] = {}
    for kind, payload_start, payload_end, offset in _iter_boxes(
        content, 0, len(content), name=name, strict=strict
    ):
        if strict and kind == b"moov" and kind in offsets:
            raise ValueError(f"{name} has duplicate moov boxes")
        if kind in {b"moov", b"mdat"} and kind not in offsets:
            offsets[kind] = offset
            spans[kind] = (payload_start, payload_end)
    if b"moov" not in offsets or b"mdat" not in offsets:
        raise ValueError(f"{name} must contain moov and mdat boxes")
    if offsets[b"moov"] > offsets[b"mdat"]:
        raise ValueError(f"{name} must be written with the moov box first (faststart)")
    moov_start, moov_end = spans[b"moov"]
    mvhd = _find_box(content, moov_start, moov_end, b"mvhd", name=name, strict=strict)
    if mvhd is None:
        raise ValueError(f"{name} has no movie header")
    if strict and mvhd[0] == mvhd[1]:
        raise ValueError(f"{name} has a truncated movie header")
    version = content[mvhd[0]]
    if strict and (version not in (0, 1) or mvhd[1] - mvhd[0] < (32 if version == 1 else 20)):
        raise ValueError(f"{name} has an invalid movie header")
    if version == 1:
        timescale = int.from_bytes(content[mvhd[0] + 20 : mvhd[0] + 24], "big")
        duration = int.from_bytes(content[mvhd[0] + 24 : mvhd[0] + 32], "big")
    else:
        timescale = int.from_bytes(content[mvhd[0] + 12 : mvhd[0] + 16], "big")
        duration = int.from_bytes(content[mvhd[0] + 16 : mvhd[0] + 20], "big")
    if timescale <= 0:
        raise ValueError(f"{name} has an invalid timescale")
    seconds = duration / timescale
    if not min_seconds <= seconds <= max_seconds:
        raise ValueError(f"{name} must run {min_seconds:g}-{max_seconds:g} seconds")
    width = height = 0
    has_video = has_audio = False
    for kind, trak_start, trak_end, _offset in _iter_boxes(
        content, moov_start, moov_end, name=name, strict=strict
    ):
        if kind != b"trak":
            continue
        mdia = _find_box(content, trak_start, trak_end, b"mdia", name=name, strict=strict)
        hdlr = (
            _find_box(content, *mdia, b"hdlr", name=name, strict=strict)
            if mdia is not None
            else None
        )
        if strict and (hdlr is None or hdlr[1] - hdlr[0] < 12):
            raise ValueError(f"{name} track has an invalid handler")
        handler = content[hdlr[0] + 8 : hdlr[0] + 12] if hdlr is not None else b""
        encoded_dimensions = (
            _validate_launch_media(content, mdia, handler, seconds)
            if strict and handler in (b"vide", b"soun")
            else None
        )
        if handler == b"soun":
            if strict and has_audio:
                raise ValueError(f"{name} must carry exactly one audio track")
            has_audio = True
        if handler != b"vide":
            continue
        if strict and has_video:
            raise ValueError(f"{name} must carry exactly one video track")
        tkhd = _find_box(content, trak_start, trak_end, b"tkhd", name=name, strict=strict)
        if tkhd is None:
            raise ValueError(f"{name} track has no header")
        if strict and (
            tkhd[0] == tkhd[1]
            or content[tkhd[0]] not in (0, 1)
            or tkhd[1] - tkhd[0] < (96 if content[tkhd[0]] == 1 else 84)
        ):
            raise ValueError(f"{name} has an invalid video track header")
        base = tkhd[0] + (88 if content[tkhd[0]] == 1 else 76)
        width = int.from_bytes(content[base : base + 4], "big") >> 16
        height = int.from_bytes(content[base + 4 : base + 8], "big") >> 16
        if strict and encoded_dimensions != (width, height):
            raise ValueError("launch video encoded dimensions differ from its track header")
        has_video = True
    if not has_video:
        raise ValueError(f"{name} has no video track")
    if (width, height) not in dimensions:
        raise ValueError(dimension_error)
    if not has_audio:
        raise ValueError(f"{name} must carry a voice track")
    return DemoVideo(duration_seconds=seconds, width=width, height=height, has_audio=has_audio)
