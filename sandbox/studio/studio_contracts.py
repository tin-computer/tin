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
CHARACTER_SVG_VALIDATOR = "character-svg.v1"
DEMO_VIDEO_VALIDATOR = "demo-video.v1"
FEATURE_FILM_VALIDATOR = "feature-film.v1"

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

# A feature film is a note (`films/<slug>.md`) that plays the film its folder carries: one
# `tin-video` block whose files sit in `<slug>.assets/`. The film's sizes are the film kit's
# formats (web/film/film.js): 4:5 and 1:1 for feeds, 16:9 for desktop, the site and blogs.
MAX_FEATURE_FILM_NOTE_BYTES = 64_000
FEATURE_FILM_SIZES = frozenset({(1080, 1350), (1080, 1080), (1920, 1080)})
FEATURE_FILM_MIN_SECONDS = 8.0
FEATURE_FILM_MAX_SECONDS = 90.0
_VIDEO_BLOCK = re.compile(r"^```tin-video[ \t]*\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)
_BLOCK_FIELD = re.compile(r"([a-z_]+):[ \t]*(.*)")
_FILM_FILE = r"[a-z0-9][a-z0-9_-]*"
# Before a film is built, the same note offers its directions: a sketch of each way to show the
# claim, one still apiece, and the one a second look recommends. The founder picks one to build
# (or asks for others), and the note's next version is the film.
_DIRECTIONS_BLOCK = re.compile(
    r"^```tin-directions[ \t]*\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL
)
_SKETCH_HEADING = re.compile(r"^## (\d+)\. (.+?)[ \t]*$", re.MULTILINE)
_HEADING = re.compile(r"^#{1,2} ", re.MULTILINE)
MIN_FILM_SKETCHES = 2
MAX_FILM_SKETCHES = 8


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


@dataclass(frozen=True)
class FeatureFilmNote:
    title: str
    video: str
    poster: str | None
    captions: str | None


@dataclass(frozen=True)
class FilmSketch:
    n: int
    name: str
    still: str
    page: str | None
    about: str


@dataclass(frozen=True)
class FilmDirections:
    title: str
    recommended: int
    runner_up: int | None
    why: str
    sketches: tuple[FilmSketch, ...]

    def view(self) -> dict:
        """The directions as the Decisions card and an agent read them."""
        return {
            "recommended": self.recommended,
            "runner_up": self.runner_up,
            "why": self.why,
            "sketches": [
                {
                    "n": item.n,
                    "name": item.name,
                    "still": item.still,
                    "page": item.page,
                    "about": item.about,
                }
                for item in self.sketches
            ],
        }


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


def _iter_boxes(data: bytes, start: int, end: int, name: str = "video"):
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


def _find_box(
    data: bytes, start: int, end: int, kind: bytes, name: str = "video"
) -> tuple[int, int] | None:
    for found, payload_start, payload_end, _offset in _iter_boxes(data, start, end, name):
        if found == kind:
            return payload_start, payload_end
    return None


def inspect_mp4(content: bytes, *, name: str = "video") -> DemoVideo:
    """Read an MP4's layout without a decoder: the moov box before mdat (fast start, so it
    plays while it loads), its video track's size, whether it has sound, and its length.
    `name` is what the errors call the file."""
    if len(content) < 64 or content[4:8] != b"ftyp":
        raise ValueError(f"{name} must be an MP4 file starting with an ftyp box")
    offsets: dict[bytes, int] = {}
    spans: dict[bytes, tuple[int, int]] = {}
    for kind, payload_start, payload_end, offset in _iter_boxes(content, 0, len(content), name):
        if kind in {b"moov", b"mdat"} and kind not in offsets:
            offsets[kind] = offset
            spans[kind] = (payload_start, payload_end)
    if b"moov" not in offsets or b"mdat" not in offsets:
        raise ValueError(f"{name} must contain moov and mdat boxes")
    if offsets[b"moov"] > offsets[b"mdat"]:
        raise ValueError(f"{name} must be written with the moov box first (faststart)")
    moov_start, moov_end = spans[b"moov"]
    mvhd = _find_box(content, moov_start, moov_end, b"mvhd", name)
    if mvhd is None:
        raise ValueError(f"{name} has no movie header")
    version = content[mvhd[0]]
    if version == 1:
        timescale = int.from_bytes(content[mvhd[0] + 20 : mvhd[0] + 24], "big")
        duration = int.from_bytes(content[mvhd[0] + 24 : mvhd[0] + 32], "big")
    else:
        timescale = int.from_bytes(content[mvhd[0] + 12 : mvhd[0] + 16], "big")
        duration = int.from_bytes(content[mvhd[0] + 16 : mvhd[0] + 20], "big")
    if timescale <= 0:
        raise ValueError(f"{name} has an invalid timescale")
    seconds = duration / timescale
    width = height = 0
    has_video = has_audio = False
    for kind, trak_start, trak_end, _offset in _iter_boxes(content, moov_start, moov_end, name):
        if kind != b"trak":
            continue
        mdia = _find_box(content, trak_start, trak_end, b"mdia", name)
        hdlr = _find_box(content, *mdia, b"hdlr", name) if mdia is not None else None
        handler = content[hdlr[0] + 8 : hdlr[0] + 12] if hdlr is not None else b""
        if handler == b"soun":
            has_audio = True
        if handler != b"vide":
            continue
        tkhd = _find_box(content, trak_start, trak_end, b"tkhd", name)
        if tkhd is None:
            raise ValueError(f"{name} track has no header")
        base = tkhd[0] + (88 if content[tkhd[0]] == 1 else 76)
        width = int.from_bytes(content[base : base + 4], "big") >> 16
        height = int.from_bytes(content[base + 4 : base + 8], "big") >> 16
        has_video = True
    if not has_video:
        raise ValueError(f"{name} has no video track")
    return DemoVideo(duration_seconds=seconds, width=width, height=height, has_audio=has_audio)


def validate_demo_video(content: bytes) -> DemoVideo:
    """Check an MP4 structurally: fast-start layout, one portrait video track, one audio track,
    and a duration inside the short-form window. No decoder runs on the switchboard."""
    if not content or len(content) > MAX_DEMO_VIDEO_BYTES:
        raise ValueError(f"demo video must contain 1-{MAX_DEMO_VIDEO_BYTES} bytes")
    video = inspect_mp4(content, name="demo video")
    if not DEMO_VIDEO_MIN_SECONDS <= video.duration_seconds <= DEMO_VIDEO_MAX_SECONDS:
        raise ValueError(
            f"demo video must run {DEMO_VIDEO_MIN_SECONDS:g}-{DEMO_VIDEO_MAX_SECONDS:g} seconds"
        )
    if (video.width, video.height) != (DEMO_VIDEO_WIDTH, DEMO_VIDEO_HEIGHT):
        raise ValueError(f"demo video must be {DEMO_VIDEO_WIDTH}x{DEMO_VIDEO_HEIGHT} portrait")
    if not video.has_audio:
        raise ValueError("demo video must carry a voice track")
    return video


def parse_feature_film(content: bytes, *, folder: str) -> FeatureFilmNote:
    """Check a film note: a title, then exactly one `tin-video` block whose video, and its
    poster and captions when named, are files in the note's own folder (`folder` is that
    folder's name, `<slug>.assets`). The files themselves are checked as they are kept."""
    if not content or len(content) > MAX_FEATURE_FILM_NOTE_BYTES:
        raise ValueError(f"film note must contain 1-{MAX_FEATURE_FILM_NOTE_BYTES} bytes")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("film note must be UTF-8") from exc
    first = text.lstrip("\ufeff").split("\n", 1)[0]
    title = first.removeprefix("# ").strip()
    if not first.startswith("# ") or not title:
        raise ValueError("film note must start with a '# ' title")
    blocks = _VIDEO_BLOCK.findall(text)
    if len(blocks) != 1:
        raise ValueError("film note must play exactly one film in a tin-video block")
    fields = {
        match[1]: match[2].strip()
        for line in blocks[0].splitlines()
        if (match := _BLOCK_FIELD.fullmatch(line.strip()))
    }
    here = rf"(?:\./)?{re.escape(folder)}/{_FILM_FILE}"

    def file(name: str, suffixes: str, label: str, *, required: bool = False) -> str | None:
        value = fields.get(name, "")
        if not value and not required:
            return None
        if not re.fullmatch(rf"{here}\.(?:{suffixes})", value):
            raise ValueError(f"the film's {name} must be {label} file in {folder}/")
        return value.removeprefix("./")

    return FeatureFilmNote(
        title=title,
        video=file("src", "mp4", "an MP4", required=True) or "",
        poster=file("poster", "png|jpg|jpeg", "a PNG or JPEG"),
        captions=file("captions", "vtt", "a WebVTT"),
    )


def is_film_directions(content: bytes) -> bool:
    """Whether a film note offers directions rather than playing a film."""
    return bool(_DIRECTIONS_BLOCK.search(content.decode("utf-8", errors="replace")))


def parse_film_directions(content: bytes, *, folder: str) -> FilmDirections:
    """Check a directions note: a title, one `tin-directions` block naming the recommended sketch
    (`recommended: 3`, optionally `runner_up:` and `why:`), and a `## N. Name` section per sketch,
    numbered from 1, each showing its still from the note's folder and, optionally, linking its
    sketch page there."""
    if not content or len(content) > MAX_FEATURE_FILM_NOTE_BYTES:
        raise ValueError(f"film note must contain 1-{MAX_FEATURE_FILM_NOTE_BYTES} bytes")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("film note must be UTF-8") from exc
    first = text.lstrip("\ufeff").split("\n", 1)[0]
    title = first.removeprefix("# ").strip()
    if not first.startswith("# ") or not title:
        raise ValueError("film note must start with a '# ' title")
    if _VIDEO_BLOCK.search(text):
        raise ValueError("a directions note plays no film: build one direction first")
    blocks = _DIRECTIONS_BLOCK.findall(text)
    if len(blocks) != 1:
        raise ValueError("a directions note has exactly one tin-directions block")
    fields = {
        match[1]: match[2].strip()
        for line in blocks[0].splitlines()
        if (match := _BLOCK_FIELD.fullmatch(line.strip()))
    }
    headings = list(_SKETCH_HEADING.finditer(text))
    if not MIN_FILM_SKETCHES <= len(headings) <= MAX_FILM_SKETCHES:
        raise ValueError(
            f"a directions note offers {MIN_FILM_SKETCHES} to {MAX_FILM_SKETCHES} sketches, "
            "each under a '## N. Name' heading"
        )
    here = rf"(?:\./)?{re.escape(folder)}/{_FILM_FILE}"
    still = re.compile(rf"!\[[^\]\n]*\]\(({here}\.(?:png|jpg|jpeg))\)")
    page = re.compile(rf"\[[^\]\n]+\]\(({here}\.html)\)")
    sketches = []
    for index, heading in enumerate(headings):
        n, name = int(heading[1]), heading[2].strip()
        if n != index + 1:
            raise ValueError("the sketches are numbered 1, 2, 3 and so on, in order")
        if len(name) > 60:
            raise ValueError(f"sketch {n}'s name is longer than 60 characters")
        # A sketch's section ends at the next heading, numbered or not.
        following = _HEADING.search(text, heading.end())
        body = text[heading.end() : following.start() if following else len(text)]
        stills = still.findall(body)
        if len(stills) != 1:
            raise ValueError(f"sketch {n} shows exactly one still, a PNG or JPEG in {folder}/")
        pages = page.findall(body)
        about = " ".join(
            line.strip()
            for line in body.splitlines()
            if line.strip()
            and not still.search(line)
            and not page.search(line)
            and not line.lstrip().startswith(("#", "```"))
        )
        sketches.append(
            FilmSketch(
                n=n,
                name=name,
                still=stills[0].removeprefix("./"),
                page=pages[0].removeprefix("./") if pages else None,
                about=about[:600],
            )
        )

    def number(key: str, *, required: bool) -> int | None:
        value = fields.get(key, "")
        if not value and not required:
            return None
        if not value.isdigit() or not 1 <= int(value) <= len(sketches):
            raise ValueError(f"tin-directions {key} must be one of the sketches' numbers")
        return int(value)

    recommended = number("recommended", required=True) or 0
    runner_up = number("runner_up", required=False)
    if runner_up == recommended:
        runner_up = None
    return FilmDirections(
        title=title,
        recommended=recommended,
        runner_up=runner_up,
        why=fields.get("why", "")[:600],
        sketches=tuple(sketches),
    )


def parse_film_output(content: bytes, *, folder: str) -> FeatureFilmNote | FilmDirections:
    """A film note's either shape: its directions, before a film is built, or the film."""
    if is_film_directions(content):
        return parse_film_directions(content, folder=folder)
    return parse_feature_film(content, folder=folder)


def validate_feature_film_video(content: bytes) -> DemoVideo:
    """The film itself: a fast-start MP4 in one of the kit's sizes, 8 to 90 seconds long."""
    video = inspect_mp4(content, name="film")
    if not FEATURE_FILM_MIN_SECONDS <= video.duration_seconds <= FEATURE_FILM_MAX_SECONDS:
        raise ValueError(
            f"film must run {FEATURE_FILM_MIN_SECONDS:g}-{FEATURE_FILM_MAX_SECONDS:g} seconds"
        )
    if (video.width, video.height) not in FEATURE_FILM_SIZES:
        sizes = ", ".join(f"{w}x{h}" for w, h in sorted(FEATURE_FILM_SIZES))
        raise ValueError(f"film must be one of {sizes}")
    return video
