"""A second look at a feature film, by a model that didn't make it.

The film workflow's agent (Codex, in the studio sandbox) asks for one through the run-bound
`POST /internal/run-tools/studio/review` route, with the take's stills, its film spec and its
check summary. The switchboard sends them to an image-reading model route with the review brief
and returns the findings, which the agent fixes in the same run. One effect receipt per request
means a retry replays the findings it already bought; a run has a small quota of reviews. The
model call itself is receipted and billed by the native model service (model_usage).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import Any
from uuid import UUID

from tin_lite.billing_contracts import BillingError
from tin_lite.db import Database
from tin_lite.failures import billing_refusal
from tin_lite.model_providers import (
    MessageRole,
    ModelCapability,
    ModelImage,
    ModelMessage,
    ModelRequest,
    ModelRoute,
    ModelRouter,
    ProviderName,
    ReasoningEffort,
)
from tin_lite.model_usage import model_usage_scope

# A different model from the one that writes the film (the run's Codex model), so the film is
# never judged only by its author.
ROUTE = ModelRoute(
    key="creative.feature_film.review.v1",
    provider=ProviderName.OPENAI,
    model="gpt-6-sol",
    capabilities=frozenset(
        {
            ModelCapability.TEXT,
            ModelCapability.JSON_SCHEMA,
            ModelCapability.REASONING_EFFORT,
            ModelCapability.IMAGE_INPUT,
        }
    ),
)
OPERATION = "studio_review"
# Runaway guards: a run reviews its film once or twice; a review reads a contact sheet and the
# marked stills.
MAX_REVIEWS_PER_RUN = 4
MAX_IMAGES = 12
MAX_IMAGE_BYTES = 4_000_000
MAX_TEXT_BYTES = 64_000
MAX_OUTPUT_TOKENS = 8_000
# What one high-detail image may cost in input tokens, for the call's reservation; the charge
# is what the provider reports.
IMAGE_TOKEN_BOUND = 3_000

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "summary", "findings"],
    "properties": {
        "verdict": {"type": "string", "enum": ["ready", "fix"]},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["where", "what", "fix"],
                "properties": {
                    "where": {"type": "string"},
                    "what": {"type": "string"},
                    "fix": {"type": "string"},
                },
            },
        },
    },
}

BRIEF = """You review a short product film before a founder posts it on LinkedIn or X. You did
not make it. You get the film's stills (a contact sheet of its marked frames, then each marked
frame at full size), its spec (the beats it plays, its captions and its story), its storyboard
(the shots it planned, one marked still each) and what Tin's frame checks still report. Often the
last image is the contact sheet of the worked example the film was built from: the bar, a film of
another feature that Tin's team made and approved.

The film is a staged scene in the product's own look, the way a launch page shows a feature:
photogenic parts (a window, the product's app, a terminal, a card) where one thing happens per
beat, with a drawn cursor and a caption band under the picture. It plays muted, so the captions
carry it.

Judge it as a viewer scrolling a feed would meet it:
- The first frame and the first caption make the change clear within a second and a half.
- Each caption says one idea, reads in the time it shows, and matches what the picture shows
  at that moment. A caption that claims something the picture doesn't show is a finding.
- One hero leads each shot, with at most one card beside it; the eye lands first on what the
  caption names. No word, button or heading is cut at the frame's edge, nothing covers the
  feature, and no column or corner sits empty.
- Real words only where the story is; the rest is quiet. Too many words, a crowded frame or a
  generic gray mock that doesn't read as this product is a finding.
- The cursor points at what the film is about and rests on empty surface at a still.
- The poster still tells the story on its own, with the result on screen.
- Text in the picture is large enough to read on a phone.
- Nothing looks broken or unfinished: an empty card, overlapping parts, a cut line of text,
  sample content that reads as a real customer or a real metric.

Hold it to its storyboard and to the bar:
- Each still is the shot its storyboard describes: the hero, the satellite, the real words and
  the change it planned. A planned move that left no trace in its still, or a shot that shows
  less than it planned, is a finding.
- Against the example's sheet: type as large, the hero inset a gutter and running off an edge
  rather than floating whole, one satellite overlapping it, as few real words, as much changing
  from shot to shot, and a scene that travels from where the founder's problem lives to the
  product answering it. Where this film falls short of the bar, say what to change. Never ask
  it to copy the example's content: it shows another feature.

Write findings the film's author can act on in its spec or its stage: where (a mark's name or a
caption), what is wrong, and the fix (a beat to add, change or remove, or a part of the stage to
change). Leave out taste that a founder could reasonably disagree with. When nothing needs
fixing, say `ready` with no findings."""


# Choosing a direction: the maker's sketches, one numbered still each, before any storyboard.
DIRECTIONS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["choice", "runner_up", "why", "keep"],
    "properties": {
        "choice": {"type": "integer", "minimum": 1, "maximum": 12},
        "runner_up": {"type": "integer", "minimum": 1, "maximum": 12},
        "why": {"type": "string"},
        "keep": {"type": "string"},
    },
}

DIRECTIONS_BRIEF = """You choose among sketches for a short film before its maker storyboards it.
You did not make them. You get one sheet of numbered stills, each the poster frame of one
direction with its numbered caption, and the maker's notes: the post's claim, the kind of film
(a product highlight, a motion graphic or both), what research found and the directions.

Choose the one a viewer scrolling a feed would understand fastest and remember:
- its frame says the post's claim even without the caption;
- it is true to what the product does, and needs no claim the notes cannot back;
- it is least like a stock picture of its subject, and not merely the maker's first habit;
- it can carry fifteen to forty seconds of motion, with something changing in every shot.

Judge the idea, not the sketch's polish: sketches are rough. A product highlight should still
look like the product. Name the runner-up. Your choice is shown to the founder as the
recommendation, beside the stills, and they pick: say `why` in one or two plain sentences to
them, about what the frame shows, never about the sketch numbers or the maker. In `keep`, tell
the maker what the chosen one must keep or fix as it becomes a film."""


def _choice(parsed: Any, sketches: int) -> dict[str, Any]:
    """The chosen direction as the sandbox gets it, shaped to fit the sketches sent."""
    if not isinstance(parsed, dict) or not isinstance(parsed.get("choice"), int):
        raise FilmReviewError("the review came back without a choice")
    choice = min(max(parsed["choice"], 1), sketches)
    runner = parsed.get("runner_up") if isinstance(parsed.get("runner_up"), int) else choice
    return {
        "verdict": "chosen",
        "choice": choice,
        "runner_up": min(max(runner, 1), sketches),
        "summary": " ".join(str(parsed.get("why", "")).split())[:800],
        "keep": " ".join(str(parsed.get("keep", "")).split())[:800],
        "findings": [],
    }


class FilmReviewError(RuntimeError):
    """A bounded, user-safe review failure (quota, validation, or provider refusal)."""


def review_execution_key(run_id: UUID, request_id: str) -> str:
    return f"{run_id}:studio-review:{request_id}"


def _image(item: Any) -> ModelImage:
    if not isinstance(item, dict) or item.get("media_type") not in {"image/png", "image/jpeg"}:
        raise FilmReviewError("each image must be a PNG or JPEG with its media_type")
    data = str(item.get("data_base64", ""))
    try:
        size = len(base64.b64decode(data, validate=True))
    except (binascii.Error, ValueError) as exc:
        raise FilmReviewError("each image must be base64") from exc
    if not size or size > MAX_IMAGE_BYTES:
        raise FilmReviewError(f"each image must be 1-{MAX_IMAGE_BYTES} bytes")
    return ModelImage(media_type=str(item["media_type"]), data_base64=data)


def _findings(parsed: Any) -> dict[str, Any]:
    """The review as the sandbox gets it, shaped to fit: a verdict, a summary, findings."""
    if not isinstance(parsed, dict):
        raise FilmReviewError("the review came back without findings")
    findings = [
        {key: " ".join(str(item.get(key, "")).split())[:400] for key in ("where", "what", "fix")}
        for item in (parsed.get("findings") or [])[:12]
        if isinstance(item, dict) and item.get("what")
    ]
    verdict = "fix" if findings else "ready"
    return {
        "verdict": verdict,
        "summary": str(parsed.get("summary", ""))[:800],
        "findings": findings,
    }


class FilmReview:
    def __init__(self, *, database: Database, router: ModelRouter | None) -> None:
        self._db = database
        self._router = router

    @property
    def enabled(self) -> bool:
        return self._router is not None

    async def review(
        self,
        *,
        run_id: UUID,
        request_id: str,
        spec: str,
        checks: str,
        images: list[Any],
        storyboard: str = "",
        example: Any = None,
        kind: str = "film",
        sketches: int = 0,
    ) -> dict[str, Any]:
        if kind not in {"film", "directions"}:
            raise FilmReviewError("a review is of a film or of its directions")
        if kind == "directions" and not 2 <= sketches <= 12:
            raise FilmReviewError("a choice of direction needs 2-12 sketches")
        if self._router is None:
            raise FilmReviewError("the film review model is not configured")
        if not request_id.isalnum() or len(request_id) > 64:
            raise FilmReviewError("request_id must be alphanumeric and at most 64 characters")
        text = len(spec.encode()) + len(checks.encode()) + len(storyboard.encode())
        if text > MAX_TEXT_BYTES or not spec.strip():
            raise FilmReviewError(
                f"the spec, storyboard and checks must hold 1-{MAX_TEXT_BYTES} bytes"
            )
        if not images or len(images) > MAX_IMAGES:
            raise FilmReviewError(f"a review reads 1-{MAX_IMAGES} stills")
        stills = tuple(_image(item) for item in images)
        bar = _image(example) if example is not None else None
        fingerprint = hashlib.sha256(
            json.dumps(
                [
                    spec,
                    checks,
                    [image.data_base64 for image in stills],
                    *([storyboard, bar.data_base64 if bar else ""] if storyboard or bar else []),
                    *([kind, sketches] if kind != "film" else []),
                ],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        key = review_execution_key(run_id, request_id)
        async with self._db.effect_lock(f"{run_id}:studio-review", "studio_review_quota") as (
            conn,
            _,
        ):
            existing = await self._db.get_effect(key, conn=conn)
            if existing is not None:
                if (existing.result or {}).get("request_digest") != fingerprint:
                    raise FilmReviewError("request_id was already used for a different review")
                if existing.status == "completed" and "review" in (existing.result or {}):
                    return {**existing.result["review"], "replayed": True}
                raise FilmReviewError(
                    "an earlier review attempt is unresolved; it is not bought again"
                )
            used = await conn.fetchval(
                "SELECT count(*) FROM effect_receipts WHERE operation=$1 AND execution_key LIKE $2",
                OPERATION,
                f"{run_id}:studio-review:%",
            )
            if used >= MAX_REVIEWS_PER_RUN:
                raise FilmReviewError("this run has used all of its reviews")
            async with conn.transaction():
                await self._db.start_effect(conn, execution_key=key, operation=OPERATION)
                await self._db.save_effect_progress(
                    conn, execution_key=key, result={"request_digest": fingerprint}
                )
            choosing = kind == "directions"
            content = (
                {
                    "notes": spec,
                    "images": f"one sheet of {sketches} numbered sketches, in order"
                    + (", then the worked example's contact sheet" if bar else ""),
                }
                if choosing
                else {
                    "spec": spec,
                    "storyboard": storyboard or "(none)",
                    "checks": checks,
                    "images": (
                        f"the film's contact sheet, then its {len(stills) - 1} marked stills"
                        + (", then the worked example's contact sheet" if bar else "")
                    ),
                }
            )
            request = ModelRequest(
                system=DIRECTIONS_BRIEF if choosing else BRIEF,
                messages=(
                    ModelMessage(
                        role=MessageRole.USER,
                        content=json.dumps(content, ensure_ascii=False),
                        images=stills + ((bar,) if bar else ()),
                    ),
                ),
                max_output_tokens=MAX_OUTPUT_TOKENS,
                reasoning_effort=ReasoningEffort.MEDIUM,
                output_schema=DIRECTIONS_SCHEMA if choosing else SCHEMA,
                output_schema_name="film_directions" if choosing else "film_review",
            )
            pictures = len(stills) + (1 if bar else 0)
            bound = len(BRIEF.encode()) + MAX_TEXT_BYTES + IMAGE_TOKEN_BOUND * pictures + 4096
            try:
                with model_usage_scope(
                    run_id=run_id, step=f"studio-review:{request_id}", conn=conn, input_tokens=bound
                ):
                    result = await self._router.generate(ROUTE.key, request)
                review = _choice(result.parsed, sketches) if choosing else _findings(result.parsed)
            except BillingError as exc:
                await self._db.fail_effect(conn, execution_key=key, error_message=str(exc))
                raise FilmReviewError(billing_refusal(exc).message) from exc
            except Exception as exc:
                reason = str(exc) if isinstance(exc, FilmReviewError) else type(exc).__name__
                await self._db.fail_effect(conn, execution_key=key, error_message=reason[:300])
                raise FilmReviewError("the review model did not return a usable review") from exc
            await self._db.complete_effect(
                conn,
                execution_key=key,
                result={"request_digest": fingerprint, "review": review, "stills": len(stills)},
            )
        return {**review, "replayed": False}
