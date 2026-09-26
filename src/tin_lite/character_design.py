"""Brand character design: prepare the product facts, ask one model for the SVG.

`creative.character` runs on the switchboard without a sandbox. Where an agent would explore
the product with a browser and rasterize its drafts to look at them, this module does the
looking up front: it fetches the product page on the switchboard, extracts the facts a designer
would notice (title, description, headings, calls to action, body text, the brand colors used
by buttons and links), pairs them with project memory and the founder's brief, and sends one
fully prepared request to the model. The model returns the concept and the SVG in one strict
JSON object; Tin validates the SVG against the same character contract the sandbox uses, sends
the validator's exact complaints back for a bounded repair, and runs one refinement pass that
plays the role of the agent's look-and-fix step. No credential, HTML, or model text leaves the
switchboard.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from tin_lite.model_providers import (
    MessageRole,
    ModelCapability,
    ModelMessage,
    ModelRequest,
    ModelRoute,
    ModelRouter,
    ProviderName,
    ReasoningEffort,
)
from tin_lite.model_usage import model_usage_step
from tin_lite.studio_contracts import (
    CHARACTER_EYE_IDS,
    CHARACTER_MOUTH_IDS,
    CHARACTER_OPTIONAL_IDS,
    MAX_CHARACTER_SVG_BYTES,
    parse_character_svg,
)

KEY = "creative.character"
ROUTE_KEY = "creative.character.v1"
EXAMPLE_CHARACTER_PATH = Path(__file__).with_name("example_character.svg")
MODEL = "gpt-6-sol"
MODEL_ROUTE = ModelRoute(
    key=ROUTE_KEY,
    provider=ProviderName.OPENAI,
    model=MODEL,
    capabilities=frozenset(
        {ModelCapability.TEXT, ModelCapability.JSON_SCHEMA, ModelCapability.REASONING_EFFORT}
    ),
)


def model_route_definition() -> dict[str, Any]:
    return {
        "key": MODEL_ROUTE.key,
        "provider": MODEL_ROUTE.provider.value,
        "model": MODEL_ROUTE.model,
        "capabilities": sorted(item.value for item in MODEL_ROUTE.capabilities),
    }


MAX_PAGE_BYTES = 1_500_000
MAX_STYLESHEET_BYTES = 400_000
MAX_STYLESHEETS = 3
MAX_PAGE_TEXT_CHARS = 6_000
MAX_MEMORY_CHARS = 8_000
MAX_REPAIR_ROUNDS = 2
FACE_BOX = (140, 200, 372, 360)  # x0, y0, x1, y1: where the renderer expects the face

_HEX_COLOR = re.compile(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_SKIP_TEXT_TAGS = frozenset({"script", "style", "noscript", "svg", "template", "head"})


class CharacterDesignError(RuntimeError):
    """A bounded, Tin-owned failure: the page could not be read or the model output is unusable."""


@dataclass(frozen=True)
class ProductPage:
    url: str
    final_url: str
    title: str
    description: str
    headings: tuple[str, ...]
    actions: tuple[str, ...]
    text: str
    colors: tuple[str, ...]
    theme_color: str

    def definition(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DesignBrief:
    project_name: str
    slug: str
    brief: str
    notes: str
    memory: str
    page: ProductPage | None


@dataclass(frozen=True)
class CharacterDesign:
    svg: bytes
    concept: dict[str, Any]
    model: str
    request_ids: tuple[str, ...]
    usage: dict[str, int | None]
    timings_ms: dict[str, int]
    repairs: int
    refined: bool
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def receipt(self) -> dict[str, Any]:
        return {
            "svg": self.svg.decode("utf-8"),
            "concept": self.concept,
            "model": self.model,
            "request_ids": list(self.request_ids),
            "usage": self.usage,
            "timings_ms": self.timings_ms,
            "repairs": self.repairs,
            "refined": self.refined,
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_receipt(cls, value: dict[str, Any]) -> CharacterDesign:
        svg = value.get("svg")
        concept = value.get("concept")
        if not isinstance(svg, str) or not svg or not isinstance(concept, dict):
            raise CharacterDesignError("stored character design is invalid")
        return cls(
            svg=svg.encode("utf-8"),
            concept=concept,
            model=str(value.get("model", MODEL)),
            request_ids=tuple(str(item) for item in value.get("request_ids", [])),
            usage={
                str(k): v if isinstance(v, int) else None for k, v in value.get("usage", {}).items()
            },
            timings_ms={str(k): int(v) for k, v in value.get("timings_ms", {}).items()},
            repairs=int(value.get("repairs", 0)),
            refined=bool(value.get("refined", False)),
            warnings=tuple(str(item) for item in value.get("warnings", [])),
        )


# --- product page -------------------------------------------------------------------------


def _validated_public_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise CharacterDesignError("product URL is invalid")
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise CharacterDesignError("product URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
    ):
        raise CharacterDesignError("product URL must be a public HTTPS URL")
    return parsed.geturl()


async def _require_public_hostname(url: str) -> None:
    hostname = urlsplit(url).hostname
    if hostname is None:
        raise CharacterDesignError("product URL has no hostname")
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(
            hostname, 443, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
        )
    except OSError as exc:
        raise CharacterDesignError("product hostname could not be resolved") from exc
    resolved = {item[4][0] for item in addresses}
    if not resolved:
        raise CharacterDesignError("product hostname could not be resolved")
    for address in resolved:
        parsed = ipaddress.ip_address(address)
        if (
            parsed.is_private
            or parsed.is_loopback
            or parsed.is_link_local
            or parsed.is_multicast
            or parsed.is_reserved
            or parsed.is_unspecified
        ):
            raise CharacterDesignError("product hostname resolves to a private address")


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.description = ""
        self.theme_color = ""
        self.headings: list[str] = []
        self.actions: list[str] = []
        self.stylesheets: list[str] = []
        self.style_text: list[str] = []
        self.text: list[str] = []
        self.structured: list[str] = []
        self._stack: list[str] = []
        self._in_json_ld = False
        self._heading: list[str] | None = None
        self._action: list[str] | None = None
        self._in_title = False
        self._in_style = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: (value or "") for key, value in attrs}
        self._stack.append(tag)
        if tag == "title":
            self._in_title = True
        elif tag == "style":
            self._in_style = True
        elif tag == "script" and "ld+json" in attributes.get("type", "").lower():
            self._in_json_ld = True
        elif tag == "meta":
            name = (attributes.get("name") or attributes.get("property") or "").lower()
            if name in {"description", "og:description"} and not self.description:
                self.description = attributes.get("content", "")
            elif name == "theme-color" and not self.theme_color:
                self.theme_color = attributes.get("content", "")
            elif name in {"og:title", "og:site_name", "keywords", "twitter:description"}:
                self.structured.append(f"{name}: {attributes.get('content', '')}"[:300])
        elif tag == "link" and "stylesheet" in attributes.get("rel", "").lower():
            href = attributes.get("href", "")
            if href:
                self.stylesheets.append(href)
        elif tag in {"h1", "h2", "h3"}:
            self._heading = []
        elif tag == "button" or (tag == "a" and attributes.get("href")):
            self._action = []
        style = attributes.get("style")
        if style:
            self.style_text.append(style)
        if tag in {"p", "li", "br", "div", "section", "h1", "h2", "h3", "h4", "tr"}:
            self.text.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_json_ld = False
        if tag == "title":
            self._in_title = False
        elif tag == "style":
            self._in_style = False
        elif tag in {"h1", "h2", "h3"} and self._heading is not None:
            heading = " ".join("".join(self._heading).split())
            if heading:
                self.headings.append(f"{tag}: {heading}"[:160])
            self._heading = None
        elif tag in {"button", "a"} and self._action is not None:
            action = " ".join("".join(self._action).split())
            if 1 < len(action) <= 60:
                self.actions.append(action)
            self._action = None
        while self._stack and self._stack.pop() != tag:
            pass

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._in_style:
            self.style_text.append(data)
            return
        if self._in_json_ld:
            self.structured.append(" ".join(data.split())[:1500])
            return
        if any(tag in _SKIP_TEXT_TAGS for tag in self._stack):
            return
        if self._heading is not None:
            self._heading.append(data)
        if self._action is not None:
            self._action.append(data)
        self.text.append(data)


def _saturated(color: str) -> bool:
    value = color.lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    r, g, b = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    high, low = max(r, g, b), min(r, g, b)
    return high - low > 40 and high > 40 and low < 235


_BRAND_SELECTOR = re.compile(
    r"button|\bbtn\b|cta|primary|accent|brand|hero|\ba\b|:root|--", re.IGNORECASE
)
_NEUTRAL_SELECTOR = re.compile(r"code|pre|hljs|token|syntax|shiki|prism|comment", re.IGNORECASE)
_RULE = re.compile(r"([^{}]{1,300})\{([^{}]*)\}")


def _normalize_hex(value: str) -> str:
    value = value.lower()
    if len(value) == 4:
        value = "#" + "".join(ch * 2 for ch in value[1:])
    return value


def _palette(page_styles: str, sheets: str, theme_color: str) -> tuple[str, ...]:
    """Brand colors, weighted by where they appear: a color on a button, call to action, or
    brand custom property outranks one buried in a syntax-highlighting theme."""
    counts: Counter[str] = Counter()

    def add(text: str, weight: float) -> None:
        for match in _HEX_COLOR.findall(text):
            value = _normalize_hex(match)
            if _saturated(value):
                counts[value] += weight

    for source, base in ((page_styles, 3.0), (sheets, 1.0)):
        for selector, body in _RULE.findall(source):
            weight = base
            if _NEUTRAL_SELECTOR.search(selector):
                weight *= 0.1
            elif _BRAND_SELECTOR.search(selector):
                weight *= 6.0
            add(body, weight)
        # Inline style attributes and anything outside a rule block.
        add(_RULE.sub("", source), base * 2.0)
    theme = _normalize_hex(theme_color) if _HEX_COLOR.fullmatch(theme_color or "") else ""
    if theme and _saturated(theme):
        counts[theme] += 40.0
    return tuple(color for color, _count in counts.most_common(8))


async def fetch_product_page(url: str) -> ProductPage:
    """Read one public HTTPS page and its first stylesheets; never a private address."""
    current = _validated_public_url(url)
    requested = current
    headers = {
        "Accept": "text/html,application/xhtml+xml",
        "User-Agent": "Mozilla/5.0 (compatible; Tin-Lite-Character/1.0)",
    }
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        body = ""
        try:
            for _hop in range(5):
                await _require_public_hostname(current)
                async with client.stream("GET", current, headers=headers) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            raise CharacterDesignError("product page redirect has no destination")
                        current = _validated_public_url(urljoin(current, location))
                        continue
                    if response.status_code >= 400:
                        raise CharacterDesignError(
                            f"product page returned HTTP {response.status_code}"
                        )
                    if "html" not in response.headers.get("content-type", "").casefold():
                        raise CharacterDesignError("product URL did not return HTML")
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_PAGE_BYTES:
                            raise CharacterDesignError("product page is too large to read")
                        chunks.append(chunk)
                    body = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
                    break
            else:
                raise CharacterDesignError("product page redirected too many times")
        except httpx.HTTPError as exc:
            raise CharacterDesignError("product page could not be read") from exc
        parser = _PageParser()
        parser.feed(body)
        page_styles = "\n".join(parser.style_text)
        sheets = ""
        for href in parser.stylesheets[:MAX_STYLESHEETS]:
            sheet_url = urljoin(current, href)
            try:
                sheet_url = _validated_public_url(sheet_url)
                await _require_public_hostname(sheet_url)
                sheet = await client.get(sheet_url, headers={"Accept": "text/css"})
            except (CharacterDesignError, httpx.HTTPError):
                continue
            if sheet.status_code == 200 and len(sheet.content) <= MAX_STYLESHEET_BYTES:
                sheets += "\n" + sheet.text
    text = " ".join("".join(parser.text).split())
    if len(text) < 200 and parser.structured:
        # A client-rendered shell: the metadata and structured data are all a plain fetch sees.
        text = (text + " " + " ".join(parser.structured)).strip()
    if len(text) > MAX_PAGE_TEXT_CHARS:
        text = text[:MAX_PAGE_TEXT_CHARS] + " …"
    actions = tuple(dict.fromkeys(parser.actions))[:20]
    return ProductPage(
        url=requested,
        final_url=current,
        title=" ".join(parser.title.split())[:200],
        description=" ".join(parser.description.split())[:500],
        headings=tuple(parser.headings[:24]),
        actions=actions,
        text=text,
        colors=_palette(page_styles, sheets, parser.theme_color.strip()),
        theme_color=parser.theme_color.strip()[:20],
    )


# --- the request ---------------------------------------------------------------------------

DESIGN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "audience": {"type": "string", "maxLength": 300},
        "product_noun": {"type": "string", "maxLength": 80},
        "concept": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "maxLength": 60},
                "species_or_object": {"type": "string", "maxLength": 120},
                "personality": {"type": "string", "maxLength": 200},
                "prop": {"type": "string", "maxLength": 120},
                "why_it_fits": {"type": "string", "maxLength": 400},
            },
            "required": ["name", "species_or_object", "personality", "prop", "why_it_fits"],
        },
        "palette": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "accent": {"type": "string", "maxLength": 9},
                "secondary": {"type": "string", "maxLength": 9},
                "skin": {"type": "string", "maxLength": 9},
                "ink": {"type": "string", "maxLength": 9},
            },
            "required": ["accent", "secondary", "skin", "ink"],
        },
        "svg": {"type": "string", "minLength": 200, "maxLength": 60_000},
    },
    "required": ["audience", "product_noun", "concept", "palette", "svg"],
}

REFINE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "problems_found": {
            "type": "array",
            "maxItems": 12,
            "items": {"type": "string", "maxLength": 200},
        },
        "svg": {"type": "string", "minLength": 200, "maxLength": 60_000},
    },
    "required": ["problems_found", "svg"],
}

SYSTEM_PROMPT = """You are a character designer for small software products. You draw one cute,
relatable vector mascot that the product's buyers would recognise as "one of us", and you
hand it over as an animatable SVG that a video renderer will flip between mouth and eye
states. You have the product facts in front of you; there is no browser and no second look,
so the drawing has to be right from geometry alone.

WHO IT IS FOR
Read the product page facts and project memory first. Name the audience in one phrase, the
noun at the centre of the product (a message, an invoice, a photo, a deploy), and the accent
color the product actually uses (from `colors` or `theme_color`; if they disagree, prefer the
color used on buttons). If the founder's brief names the character, honour it. Otherwise
choose a creature or object that puns on the product name or its noun, give it one prop the
audience recognises, and one personality trait. Prefer a creature with a face over an object.

THE SVG CONTRACT (checked mechanically; any violation is rejected)
- Root `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512"
  height="512" role="img" aria-label="...">`.
- Pure geometry only: path, circle, ellipse, rect, line, polyline, polygon, g, defs,
  linearGradient, radialGradient, stop, filter, feGaussianBlur, and one <style> block with
  class selectors. Never text, tspan, image, use, a, script, foreignObject, animation
  elements, event attributes, or url() that is not a #fragment. No DOCTYPE, no entities.
- Exactly these five <g> groups, each once, with these ids: mouth-closed, mouth-mid,
  mouth-open, eyes-open, eyes-closed. Optionally a sixth: expr-happy. Put display="none" on
  mouth-mid, mouth-open, eyes-closed and expr-happy so the resting face shows by default.
- The three mouths share one anchor point on the face. mouth-closed is a smile stroke;
  mouth-mid is a small open shape with an inner tongue or throat color; mouth-open is a
  taller open shape. They must look clearly different at 120 px wide. eyes-closed is two
  happy arcs at the eye centres. expr-happy adds a reaction (sparkles, a delivered check
  bubble, a raised prop) that never covers the face.
- Eyes and mouth inside the box x 140-372, y 200-360. The renderer seats the character on the
  bottom-right corner of a phone card, so the face must stay visible; a tail, feet or base
  may hang off the canvas edge.
- Under 60,000 characters and under 1,500 elements. Integer coordinates.

HOW TO DRAW IT WELL
- Outline style: every silhouette gets one ink stroke (the palette's ink, about #1B1B1F) of
  width 6-8 with round caps and joins; declare it once as a class such as `.ink` and reuse it.
- Big head or body, eyes at least 10% of the canvas each with a pure white (#FFFFFF) eye
  white, a dark pupil and a small white highlight. Brows above the eyes, blush on the cheeks.
  Body and prop fills: two or three tints of the brand accent plus one warm skin or belly
  tone; white, the ink color, and lighter or darker tints of the palette are always allowed.
  At most one gradient, soft top-to-bottom.
- Draw order, back to front: ground shadow, back limbs and tail, body, belly or face plate,
  antennae or hair, eyes-open, eyes-closed, brows, blush, the three mouth groups, front limbs
  and the prop, expr-happy last.
- The silhouette must read at thumbnail size: one big simple shape with two or three
  recognisable details, not many small ones.
- Keep it friendly: eyes level, mouth centred under them, nothing sharp near the face.

OUTPUT
Return only the JSON object the schema describes. Put the whole SVG in `svg`; do not wrap it
in Markdown. The example character in the input shows the expected level of finish and the
exact group layout; do not copy its species, pose, or palette.
"""

REFINE_PROMPT = """You are reviewing a vector character you drew, the way a designer looks at
the rendered states before shipping. You cannot render it, so check the geometry instead.
Fix only what the checklist finds; keep the concept, palette, ids and group layout.

CHECKLIST
1. Both eyes at the same height, symmetric around the face centre, inside x 140-372,
   y 200-360; pupils inside the eye whites; highlights inside the pupils.
2. The three mouth groups share one anchor and sit centred under the eyes, inside the face
   box, and differ clearly in height (closed stroke, small open, tall open).
3. eyes-closed arcs are centred on the eye whites and about the same width as them.
4. Brows sit above the eyes and do not merge with the head outline or the eyes.
5. Nothing in expr-happy overlaps the eyes or mouth; hidden groups carry display="none".
6. Every silhouette has the ink stroke; the ground shadow is under the body; front limbs and
   the prop are drawn after the body and face. Eye whites and highlights stay pure white:
   white, the ink color, and tints of the palette are all correct colors, never "fix" them.
   Change nothing that is not on this list; if the drawing passes, return it unchanged.
7. Coordinates are integers; the file keeps the contract (root attributes, allowed
   elements only, the five required ids exactly once).

Return the corrected SVG and the list of problems you actually fixed (empty if none).
"""


def _svg_bytes(value: Any) -> bytes:
    if not isinstance(value, str):
        raise CharacterDesignError("model returned no SVG")
    text = value.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    return text.strip().encode("utf-8")


def _group_numbers(element) -> list[tuple[float, float]]:
    """Every (x, y) pair mentioned inside a group, from cx/cy, x/y, and path data."""
    points: list[tuple[float, float]] = []
    for node in element.iter():
        attributes = node.attrib
        if "cx" in attributes and "cy" in attributes:
            try:
                points.append((float(attributes["cx"]), float(attributes["cy"])))
            except ValueError:
                pass
        if "x" in attributes and "y" in attributes:
            try:
                points.append((float(attributes["x"]), float(attributes["y"])))
            except ValueError:
                pass
        data = attributes.get("d")
        if data and not re.search(r"[a-z]", data.replace("e", "")):
            numbers = [float(item) for item in _NUMBER.findall(data)]
            points.extend(zip(numbers[0::2], numbers[1::2], strict=False))
    return points


def geometry_problems(svg: bytes) -> list[str]:
    """Mechanical checks that stand in for looking at the render."""
    root, _summary = parse_character_svg(svg)
    problems: list[str] = []
    groups = {
        element.get("id"): element
        for element in root.iter("{http://www.w3.org/2000/svg}g")
        if element.get("id") in {*CHARACTER_MOUTH_IDS, *CHARACTER_EYE_IDS, *CHARACTER_OPTIONAL_IDS}
    }
    x0, y0, x1, y1 = FACE_BOX
    for name in (*CHARACTER_MOUTH_IDS, *CHARACTER_EYE_IDS):
        points = _group_numbers(groups[name])
        if not points:
            problems.append(
                f"{name} has no absolute coordinates to check; use absolute path commands"
            )
            continue
        xs = [x for x, _y in points]
        ys = [y for _x, y in points]
        if min(xs) < x0 - 12 or max(xs) > x1 + 12 or min(ys) < y0 - 12 or max(ys) > y1 + 12:
            problems.append(
                f"{name} spans x {min(xs):.0f}-{max(xs):.0f}, y {min(ys):.0f}-{max(ys):.0f}; "
                f"the face must stay inside x {x0}-{x1}, y {y0}-{y1}"
            )
    centres = {}
    for name in CHARACTER_MOUTH_IDS:
        points = _group_numbers(groups[name])
        if points:
            centres[name] = sum(x for x, _y in points) / len(points)
    if len(centres) == 3 and max(centres.values()) - min(centres.values()) > 40:
        problems.append("the three mouth groups are not centred on the same anchor")
    for name in ("mouth-mid", "mouth-open", "eyes-closed", "expr-happy"):
        element = groups.get(name)
        if element is not None and element.get("display") != "none":
            problems.append(
                f'{name} must carry display="none" so the resting face shows by default'
            )
    return problems


class CharacterDesigner:
    def __init__(self, *, router: ModelRouter, example_svg: bytes | None = None) -> None:
        self._router = router
        self._example = (example_svg or EXAMPLE_CHARACTER_PATH.read_bytes()).decode("utf-8")

    async def design(self, brief: DesignBrief, *, route_key: str = ROUTE_KEY) -> CharacterDesign:
        request_ids: list[str] = []
        usage: Counter[str] = Counter()
        timings: dict[str, int] = {}
        warnings: list[str] = []

        async def call(label: str, request: ModelRequest) -> dict[str, Any]:
            started = time.monotonic()
            with model_usage_step(label):
                result = await self._router.generate(route_key, request)
            timings[label] = int((time.monotonic() - started) * 1000)
            if result.request_id:
                request_ids.append(result.request_id)
            for key, value in asdict(result.usage).items():
                if isinstance(value, int):
                    usage[key] += value
            if not isinstance(result.parsed, dict):
                raise CharacterDesignError(f"model returned no structured {label} result")
            return result.parsed

        reference = {
            "project_name": brief.project_name,
            "character_slug": brief.slug,
            "founder_brief": brief.brief or "(none: derive the character from the product)",
            "founder_notes": brief.notes or "(none)",
            "project_memory": brief.memory or "(no project memory yet)",
            "product_page": brief.page.definition() if brief.page else "(no page was readable)",
            "example_character_svg": self._example,
        }
        payload = json.dumps(reference, ensure_ascii=False, separators=(",", ":"))
        parsed = await call(
            "draft",
            ModelRequest(
                system=SYSTEM_PROMPT,
                messages=(ModelMessage(role=MessageRole.USER, content=payload),),
                max_output_tokens=24_000,
                reasoning_effort=ReasoningEffort.MEDIUM,
                output_schema=DESIGN_SCHEMA,
                output_schema_name="character_design",
            ),
        )
        concept = {
            key: parsed.get(key) for key in ("audience", "product_noun", "concept", "palette")
        }
        svg = _svg_bytes(parsed.get("svg"))
        repairs = 0
        problems = self._problems(svg)
        while problems and repairs < MAX_REPAIR_ROUNDS:
            repairs += 1
            parsed = await call(
                f"repair{repairs}",
                ModelRequest(
                    system=SYSTEM_PROMPT,
                    messages=(
                        ModelMessage(role=MessageRole.USER, content=payload),
                        ModelMessage(role=MessageRole.ASSISTANT, content=svg.decode("utf-8")),
                        ModelMessage(
                            role=MessageRole.USER,
                            content=(
                                "Tin rejected that SVG. Fix exactly these problems and return the "
                                "complete corrected SVG with the same concept:\n- "
                                + "\n- ".join(problems)
                            ),
                        ),
                    ),
                    max_output_tokens=24_000,
                    reasoning_effort=ReasoningEffort.MEDIUM,
                    output_schema=REFINE_SCHEMA,
                    output_schema_name="character_repair",
                ),
            )
            svg = _svg_bytes(parsed.get("svg"))
            problems = self._problems(svg)
        if problems:
            raise CharacterDesignError(
                "character SVG still violates the contract: " + "; ".join(problems[:4])
            )
        refined = False
        try:
            parsed = await call(
                "refine",
                ModelRequest(
                    system=REFINE_PROMPT,
                    messages=(
                        ModelMessage(
                            role=MessageRole.USER,
                            content=json.dumps(
                                {"concept": concept, "svg": svg.decode("utf-8")},
                                ensure_ascii=False,
                            ),
                        ),
                    ),
                    max_output_tokens=24_000,
                    reasoning_effort=ReasoningEffort.MEDIUM,
                    output_schema=REFINE_SCHEMA,
                    output_schema_name="character_refine",
                ),
            )
            candidate = _svg_bytes(parsed.get("svg"))
            if not self._problems(candidate):
                svg = candidate
                refined = True
                fixed = [str(item) for item in parsed.get("problems_found", []) if item]
                if fixed:
                    warnings.append("refinement fixed: " + "; ".join(fixed)[:400])
            else:
                warnings.append("refinement pass was discarded: it broke the contract")
        except CharacterDesignError as exc:
            warnings.append(f"refinement pass was discarded: {exc}")
        return CharacterDesign(
            svg=svg,
            concept=concept,
            model=MODEL,
            request_ids=tuple(request_ids),
            usage=dict(usage),
            timings_ms=timings,
            repairs=repairs,
            refined=refined,
            warnings=tuple(warnings),
        )

    @staticmethod
    def _problems(svg: bytes) -> list[str]:
        if len(svg) > MAX_CHARACTER_SVG_BYTES:
            return [f"the SVG must stay under {MAX_CHARACTER_SVG_BYTES} bytes"]
        try:
            return geometry_problems(svg)
        except ValueError as exc:
            return [str(exc)]
