"""Look for stray text in a finished drawing before the library keeps it.

The prompt bans text, letters, numbers, logos, watermarks and signatures, but image models still
draw them now and then (one bake-off model wrote the words "No text present in image"). A vision
model reads each raster drawing; a vector drawing is read as source. A drawing with text fails
the check and is drawn once more, as with the ground and shape checks.

The check never fails a run on its own: without an Anthropic key, or when the call fails, the
drawing is kept and its note says the text was not checked.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from anthropic import AnthropicError, AsyncAnthropic

LOGGER = logging.getLogger(__name__)

MODEL = "claude-sonnet-5-5"
# Anthropic takes images up to 5 MB; Files already caps a PNG or JPEG at 5 MB.
MAX_IMAGE_BYTES = 5 * 1024 * 1024
RASTER = {"image/png", "image/jpeg"}
SVG_TEXT = re.compile(r"<\s*(?:svg:)?text\b", re.IGNORECASE)

SCHEMA = {
    "type": "object",
    "properties": {
        "has_text": {"type": "boolean"},
        "text": {"type": "string"},
    },
    "required": ["has_text", "text"],
    "additionalProperties": False,
}

QUESTION = (
    "This drawing was made for: {subject}. It must contain no text of any kind. "
    "Look closely, including the edges, corners and small or faint marks. "
    "Does it contain any letters, words, numbers, captions, labels, signatures, watermarks, "
    "or logos with letters in them? Shapes that only look like writing from far away count "
    "when you can read a letter in them. Answer has_text, and in text quote what you can read "
    "(empty when there is none)."
)


@dataclass(frozen=True)
class TextCheck:
    """The result: `problem` is set when text was found, `note` when the check could not run."""

    problem: str | None = None
    note: str | None = None


class StrayTextChecker:
    def __init__(self, *, settings: Any = None, client: Any | None = None) -> None:
        self._client = client
        if self._client is None:
            key = getattr(settings, "anthropic_api_key", None)
            secret = key.get_secret_value() if key is not None else ""
            if secret:
                options: dict[str, Any] = {"api_key": secret, "timeout": 60.0, "max_retries": 2}
                workspace = getattr(settings, "anthropic_workspace_id", None)
                if workspace:
                    options["default_headers"] = {"anthropic-workspace-id": workspace}
                self._client = AsyncAnthropic(**options)

    async def check(self, *, content: bytes, media_type: str, subject: str) -> TextCheck:
        if media_type == "image/svg+xml":
            source = content.decode("utf-8", errors="replace")
            if SVG_TEXT.search(source):
                return TextCheck(problem="the image has stray text (a text element in the SVG)")
            return TextCheck()
        if media_type not in RASTER:
            return TextCheck(note="text not checked: unknown image type")
        if len(content) > MAX_IMAGE_BYTES:
            return TextCheck(note="text not checked: the image is over 5 MB")
        if self._client is None:
            return TextCheck(note="text not checked: no vision model is configured")
        try:
            response = await self._client.messages.create(
                model=MODEL,
                max_tokens=300,
                output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type,
                                    "data": base64.b64encode(content).decode("ascii"),
                                },
                            },
                            {"type": "text", "text": QUESTION.format(subject=subject)},
                        ],
                    }
                ],
            )
            text = "".join(
                str(block.text)
                for block in response.content
                if getattr(block, "type", None) == "text"
            )
            answer = json.loads(text)
        except (AnthropicError, ValueError, TypeError) as error:
            LOGGER.warning("stray text check failed: %s", type(error).__name__)
            return TextCheck(note="text not checked: the vision check failed")
        if not answer.get("has_text"):
            return TextCheck()
        found = " ".join(str(answer.get("text") or "").split())[:80]
        return TextCheck(
            problem=f"the image has stray text: {found}" if found else "the image has stray text"
        )
