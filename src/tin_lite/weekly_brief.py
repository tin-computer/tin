from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

MAX_WEEKLY_SOURCE_BYTES = 250_000
MAX_WEEKLY_BRIEF_BYTES = 120_000


class ResponsesClient(Protocol):
    model: str

    async def create(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class WeeklyBriefProtocolError(RuntimeError):
    """The model returned data outside the weekly-brief contract."""


@dataclass(frozen=True)
class WeeklyBriefSource:
    label: str
    artifact_ref: str
    content: str


class WeeklyBriefReporter:
    def __init__(self, *, responses: ResponsesClient, skill_suite: str) -> None:
        self._responses = responses
        self._skill_suite = skill_suite

    async def report(
        self,
        *,
        project_name: str,
        period_start: datetime,
        period_end: datetime,
        inputs: dict[str, Any],
        sources: list[WeeklyBriefSource],
    ) -> dict[str, Any]:
        source_bytes = sum(len(source.content.encode()) for source in sources)
        if source_bytes > MAX_WEEKLY_SOURCE_BYTES:
            raise ValueError(f"weekly brief sources exceed {MAX_WEEKLY_SOURCE_BYTES} bytes")
        payload = {
            "project_name": project_name,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "preferences": inputs,
            "sources": [
                {
                    "label": source.label,
                    "artifact_ref": source.artifact_ref,
                    "content": source.content,
                }
                for source in sources
            ],
        }
        response = await self._responses.create(
            {
                "instructions": self._skill_suite,
                "input": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                "store": False,
                "text": {"verbosity": "low"},
            }
        )
        body = _output_text(response)
        body = ("\n" + body).split("\n## Sources\n", 1)[0].strip()
        if not body:
            raise WeeklyBriefProtocolError("weekly brief response returned no summary")
        if not body.startswith("# "):
            body = f"# Weekly brief\n\n{body}"
        source_lines = (
            "\n".join(f"- {source.label}: `{source.artifact_ref}`" for source in sources)
            if sources
            else "- No recorded project events in this period."
        )
        markdown = f"{body}\n\n## Sources\n\n{source_lines}\n"
        encoded = markdown.encode()
        validate_weekly_brief(encoded, sources=sources)
        return {
            "response_id": _response_id(response),
            "model": self._responses.model,
            "markdown": markdown,
            "source_refs": [source.artifact_ref for source in sources],
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "usage": response.get("usage") if isinstance(response.get("usage"), dict) else {},
        }

    @staticmethod
    def build_artifacts(
        *,
        run_id: str,
        artifact_path: str,
        evidence_path: str,
        result: dict[str, Any],
    ) -> tuple[bytes, bytes]:
        markdown = str(result["markdown"]).encode()
        evidence = (
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "artifact_path": artifact_path,
                    "period_start": result["period_start"],
                    "period_end": result["period_end"],
                    "source_refs": result["source_refs"],
                    "model": result["model"],
                    "response_id": result["response_id"],
                    "usage": result["usage"],
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode()
        return markdown, evidence


def validate_weekly_brief(content: bytes, *, sources: list[WeeklyBriefSource]) -> None:
    if not content or len(content) > MAX_WEEKLY_BRIEF_BYTES:
        raise ValueError(f"weekly brief must contain between 1 and {MAX_WEEKLY_BRIEF_BYTES} bytes")
    text = content.decode("utf-8")
    if not text.startswith("# ") or "\n## Sources\n" not in text:
        raise ValueError("weekly brief must be Markdown with a Sources section")
    if any(source.artifact_ref not in text for source in sources):
        raise ValueError("weekly brief omitted a durable source reference")


def validate_weekly_brief_artifacts(
    content: bytes,
    evidence: bytes,
    *,
    artifact_path: str,
    evidence_path: str,
) -> None:
    if not evidence or len(evidence) > 500_000:
        raise ValueError("weekly brief evidence has an invalid size")
    payload = json.loads(evidence)
    if payload.get("artifact_path") != artifact_path or not evidence_path.endswith(
        "/evidence.json"
    ):
        raise ValueError("weekly brief evidence points to the wrong artifact")
    source_refs = payload.get("source_refs")
    if not isinstance(source_refs, list) or not all(isinstance(item, str) for item in source_refs):
        raise ValueError("weekly brief evidence has invalid source references")
    validate_weekly_brief(
        content,
        sources=[
            WeeklyBriefSource(label="durable source", artifact_ref=ref, content="")
            for ref in source_refs
        ],
    )


def _output_text(response: dict[str, Any]) -> str:
    output = response.get("output")
    if not isinstance(output, list):
        raise WeeklyBriefProtocolError("weekly brief response has no output list")
    fragments: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        parts = item.get("content")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "output_text":
                text = part.get("text")
                if isinstance(text, str):
                    fragments.append(text)
    result = "\n".join(fragments).strip()
    if not result:
        raise WeeklyBriefProtocolError("weekly brief response returned no Markdown")
    return result


def _response_id(response: dict[str, Any]) -> str:
    value = response.get("id")
    if not isinstance(value, str) or not value:
        raise WeeklyBriefProtocolError("weekly brief response has no ID")
    return value
