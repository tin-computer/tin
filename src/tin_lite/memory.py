from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

# Both go whole into one model's context (the sources as the gardener's input, the index
# into every later session that reads wiki/INDEX.md), so each stays within ~200k tokens
# at ~3.5 bytes a token.
MAX_MEMORY_BYTES = 700_000
MAX_SOURCE_BYTES = 700_000
OWNED_SECTION_HEADING = "## Product"
SOURCES_HEADING = "## Sources"


class ResponsesClient(Protocol):
    async def create(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class MemoryProtocolError(RuntimeError):
    """The memory model returned a response outside the gardener contract."""


@dataclass(frozen=True)
class MemorySource:
    run_id: UUID
    workflow_key: str
    artifact_ref: str
    content: str


class MemoryGardener:
    def __init__(self, *, responses: ResponsesClient, skill_suite: str) -> None:
        self._responses = responses
        self._skill_suite = skill_suite

    async def garden(
        self,
        *,
        project_name: str,
        sources: list[MemorySource],
        owned_section: str | None = None,
    ) -> bytes:
        """Regenerate the index; `owned_section` is re-inserted verbatim, never regenerated."""
        if not sources:
            return splice_owned_section(_empty_index(project_name), owned_section).encode()
        sources = newest_sources(sources)
        payload = {
            "project_name": project_name,
            "sources": [
                {
                    "run_id": str(source.run_id),
                    "workflow": source.workflow_key,
                    "artifact_ref": source.artifact_ref,
                    "content": source.content,
                }
                for source in sources
            ],
        }
        response = await self._responses.create(
            {
                "instructions": self._skill_suite,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": json.dumps(payload, separators=(",", ":")),
                            }
                        ],
                    }
                ],
                "store": False,
                "text": {"verbosity": "low"},
            }
        )
        content = splice_owned_section(_output_text(response), owned_section).encode()
        validate_memory_index(content, sources=sources)
        return content


def newest_sources(
    sources: list[MemorySource], budget: int = MAX_SOURCE_BYTES
) -> list[MemorySource]:
    """The newest sources that fit one gardener call together, oldest first like `sources`.

    Sources arrive oldest first. An older one that no longer fits is left for a later
    index to forget, instead of failing the whole run; only when not even one fits does
    gardening stop.
    """
    kept: list[MemorySource] = []
    total = 0
    for source in reversed(sources):
        size = len(source.content.encode())
        if total + size > budget:
            continue
        kept.append(source)
        total += size
    if not kept:
        raise ValueError(f"memory sources exceed {budget} bytes")
    return kept[::-1]


def validate_memory_index(content: bytes, *, sources: list[MemorySource]) -> None:
    if not content or len(content) > MAX_MEMORY_BYTES:
        raise ValueError(f"memory index must contain between 1 and {MAX_MEMORY_BYTES} bytes")
    text = content.decode("utf-8")
    if not text.startswith("# ") or "\n## Sources\n" not in text:
        raise ValueError("memory index must be Markdown with a Sources section")
    missing = [source.artifact_ref for source in sources if source.artifact_ref not in text]
    if missing:
        raise ValueError("memory index omitted a durable source reference")


def extract_owned_section(text: str) -> str | None:
    """The workflow-owned `## Product` block of an index, or None when it has none.

    The block runs from its heading to the next H2 and is returned without trailing blank
    lines, so it can be re-inserted unchanged after the gardener rewrites the rest.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    starts = [index for index, line in enumerate(lines) if line.rstrip() == OWNED_SECTION_HEADING]
    if not starts:
        return None
    start = starts[0]
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    block = lines[start:end]
    while block and not block[-1].strip():
        block.pop()
    return "\n".join(block)


def splice_owned_section(text: str, owned_section: str | None) -> str:
    """Drop any `## Product` block the model wrote and insert the preserved one before Sources."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while True:
        starts = [
            index for index, line in enumerate(lines) if line.rstrip() == OWNED_SECTION_HEADING
        ]
        if not starts:
            break
        start = starts[0]
        end = next(
            (index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")),
            len(lines),
        )
        del lines[start:end]
    if owned_section is None:
        return "\n".join(lines)
    block = [*owned_section.rstrip("\n").split("\n"), ""]
    sources = [index for index, line in enumerate(lines) if line.rstrip() == SOURCES_HEADING]
    if sources:
        insert_at = sources[0]
        lines[insert_at:insert_at] = block
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(block)
    result = "\n".join(lines)
    return result if result.endswith("\n") else result + "\n"


def _empty_index(project_name: str) -> str:
    safe_name = " ".join(project_name.split()) or "Project"
    return (
        f"# {safe_name} memory\n\n"
        "No durable workflow outputs have been recorded yet.\n\n"
        "## Sources\n\n"
        "- No durable sources yet.\n"
    )


def _output_text(response: dict[str, Any]) -> str:
    output = response.get("output")
    if not isinstance(output, list):
        raise MemoryProtocolError("memory gardener response has no output list")
    fragments: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "output_text":
                text = part.get("text")
                if isinstance(text, str):
                    fragments.append(text)
    result = "\n".join(fragments).strip()
    if not result:
        raise MemoryProtocolError("memory gardener returned no Markdown")
    return result + "\n"
