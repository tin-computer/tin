"""A film's note in project Files (`films/<slug>.md`) and the files beside it.

The founder's coding agent makes the film (docs/feature-films.md) and uploads the note with its
folder, `films/<slug>.assets/`. Before a film is built the note offers directions: a sketch of
each way to show the idea, one still apiece, and the one the agent recommends. The founder picks
one, and the agent's next version is the film itself: one `tin-video` block.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from tin_lite.studio_contracts import DemoVideo, inspect_mp4

MAX_NOTE_BYTES = 64_000
# The film kit's formats (web/film/film.js): 4:5 and 1:1 for feeds, 16:9 for desktop, the site
# and blogs, 9:16 for a phone held upright.
FILM_SIZES = frozenset({(1080, 1350), (1080, 1080), (1920, 1080), (1080, 1920)})
MIN_SECONDS = 4.0
MAX_SECONDS = 180.0
MIN_SKETCHES = 2
MAX_SKETCHES = 8

_VIDEO_BLOCK = re.compile(r"^```tin-video[ \t]*\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)
_DIRECTIONS_BLOCK = re.compile(
    r"^```tin-directions[ \t]*\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL
)
_BLOCK_FIELD = re.compile(r"([a-z_]+):[ \t]*(.*)")
_FILE = r"[a-z0-9][a-z0-9_-]*"
_SKETCH_HEADING = re.compile(r"^## (\d+)\. (.+?)[ \t]*$", re.MULTILINE)
_HEADING = re.compile(r"^#{1,2} ", re.MULTILINE)


@dataclass(frozen=True)
class Film:
    title: str
    video: str
    poster: str | None
    captions: str | None

    @property
    def files(self) -> tuple[str, ...]:
        return tuple(path for path in (self.video, self.poster, self.captions) if path)


@dataclass(frozen=True)
class Sketch:
    n: int
    name: str
    still: str
    page: str | None
    about: str


@dataclass(frozen=True)
class Directions:
    title: str
    recommended: int
    runner_up: int | None
    why: str
    sketches: tuple[Sketch, ...]

    @property
    def files(self) -> tuple[str, ...]:
        return tuple(path for item in self.sketches for path in (item.still, item.page) if path)

    def sketch(self, n: int) -> Sketch:
        return next(item for item in self.sketches if item.n == n)

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


def _text(content: bytes) -> tuple[str, str]:
    if not content or len(content) > MAX_NOTE_BYTES:
        raise ValueError(f"the note must hold 1 to {MAX_NOTE_BYTES} bytes")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("the note must be UTF-8") from exc
    first = text.lstrip("﻿").split("\n", 1)[0]
    title = first.removeprefix("# ").strip()
    if not first.startswith("# ") or not title:
        raise ValueError("the note must start with a '# ' title")
    return text, title


def _fields(block: str) -> dict[str, str]:
    return {
        match[1]: match[2].strip()
        for line in block.splitlines()
        if (match := _BLOCK_FIELD.fullmatch(line.strip()))
    }


def parse_film(content: bytes, *, folder: str) -> Film:
    """A film note: a title, then exactly one `tin-video` block whose video, and its poster and
    captions when named, are files in the note's own folder (`folder` is that folder's name,
    `<slug>.assets`)."""
    text, title = _text(content)
    blocks = _VIDEO_BLOCK.findall(text)
    if len(blocks) != 1:
        raise ValueError("a film note plays exactly one film, in one tin-video block")
    fields = _fields(blocks[0])
    here = rf"(?:\./)?{re.escape(folder)}/{_FILE}"

    def file(name: str, suffixes: str, label: str, *, required: bool = False) -> str | None:
        value = fields.get(name, "")
        if not value and not required:
            return None
        if not re.fullmatch(rf"{here}\.(?:{suffixes})", value):
            raise ValueError(f"the film's {name} must be {label} file in {folder}/")
        return value.removeprefix("./")

    return Film(
        title=title,
        video=file("src", "mp4", "an MP4", required=True) or "",
        poster=file("poster", "png|jpg|jpeg", "a PNG or JPEG"),
        captions=file("captions", "vtt", "a WebVTT"),
    )


def is_directions(content: bytes) -> bool:
    """Whether a note offers directions rather than playing a film."""
    return bool(_DIRECTIONS_BLOCK.search(content.decode("utf-8", errors="replace")))


def parse_directions(content: bytes, *, folder: str) -> Directions:
    """A directions note: a title, one `tin-directions` block naming the recommended sketch
    (`recommended: 3`, optionally `runner_up:` and `why:`), and a `## N. Name` section per sketch,
    numbered from 1, each showing its still from the note's folder and, optionally, linking its
    sketch page there."""
    text, title = _text(content)
    if _VIDEO_BLOCK.search(text):
        raise ValueError("a directions note plays no film: build one direction first")
    blocks = _DIRECTIONS_BLOCK.findall(text)
    if len(blocks) != 1:
        raise ValueError("a directions note has exactly one tin-directions block")
    fields = _fields(blocks[0])
    headings = list(_SKETCH_HEADING.finditer(text))
    if not MIN_SKETCHES <= len(headings) <= MAX_SKETCHES:
        raise ValueError(
            f"a directions note offers {MIN_SKETCHES} to {MAX_SKETCHES} sketches, "
            "each under a '## N. Name' heading"
        )
    here = rf"(?:\./)?{re.escape(folder)}/{_FILE}"
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
            Sketch(
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
    return Directions(
        title=title,
        recommended=recommended,
        runner_up=runner_up,
        why=fields.get("why", "")[:600],
        sketches=tuple(sketches),
    )


def parse_note(content: bytes, *, folder: str) -> Film | Directions:
    """A film note's either shape: its directions, before a film is built, or the film."""
    if is_directions(content):
        return parse_directions(content, folder=folder)
    return parse_film(content, folder=folder)


def validate_video(content: bytes) -> DemoVideo:
    """The film itself: a fast-start MP4 in one of the kit's sizes."""
    video = inspect_mp4(content, name="film")
    if not MIN_SECONDS <= video.duration_seconds <= MAX_SECONDS:
        raise ValueError(f"the film must run {MIN_SECONDS:g} to {MAX_SECONDS:g} seconds")
    if (video.width, video.height) not in FILM_SIZES:
        sizes = ", ".join(f"{w}x{h}" for w, h in sorted(FILM_SIZES))
        raise ValueError(f"the film must be one of {sizes}, not {video.width}x{video.height}")
    return video
