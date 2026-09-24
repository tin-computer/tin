"""Small, content-free identities for validated procedure output.

These facts identify stored bytes; they are not a second workflow state machine.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlencode

from tin_lite.domain import WorkflowRun

if TYPE_CHECKING:
    from tin_lite.code_storage import CodeStorage

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
OUTPUT_REASONS = frozenset(
    {"publication_pending", "reconciliation_pending", "output_conflict", "execution_interrupted"}
)
RETAINED_OUTPUT_EXECUTORS = frozenset({"codex.procedure", "style.capture", "workflow.code"})


def output_checkpoint_key(run: WorkflowRun) -> str:
    suffix = (
        "style_artifact_persist"
        if run.executor == "style.capture"
        else "procedure_artifact_persist"
    )
    return f"{run.id}:{suffix}"


class OutputConflictError(RuntimeError):
    """The destination changed since this run read its project snapshot."""


class PublicationPendingError(RuntimeError):
    """No safe conclusion about an earlier publication can yet be drawn."""


class StaleOutputComparisonError(RuntimeError):
    """The reviewed project revision moved; no replacement was applied by this attempt."""


# The demo video is the one declared binary output; every other checkpoint is UTF-8 text.
BINARY_MEDIA_TYPES = frozenset({"video/mp4"})
MAX_TEXT_OUTPUT_BYTES = 1_000_000
MAX_BINARY_OUTPUT_BYTES = 16_000_000


@dataclass(frozen=True)
class OutputCheckpoint:
    run_id: str
    project_id: str
    generation: int
    definition_commit_sha: str
    source_base_sha: str
    ephemeral_commit_sha: str
    artifact_path: str
    media_type: str
    sha256: str
    byte_count: int
    version: int = 1
    companions: tuple[OutputCheckpoint, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("companions")
        if self.companions:
            value["companions"] = [item.to_dict() for item in self.companions]
        return value

    @property
    def files(self) -> tuple[OutputCheckpoint, ...]:
        return (self, *self.companions)

    @classmethod
    def create(
        cls,
        *,
        run: WorkflowRun,
        revision: str,
        path: str,
        media_type: str,
        content: bytes,
        companions: tuple[OutputCheckpoint, ...] = (),
    ) -> OutputCheckpoint:
        result = cls(
            run_id=str(run.id),
            project_id=str(run.project_id),
            generation=run.generation,
            definition_commit_sha=run.definition_commit_sha or "",
            source_base_sha=run.expected_head_sha or "",
            ephemeral_commit_sha=revision,
            artifact_path=path,
            media_type=media_type,
            sha256=hashlib.sha256(content).hexdigest(),
            byte_count=len(content),
            version=2 if companions else 1,
            companions=companions,
        )
        return cls.load(result.to_dict(), run=run)

    @classmethod
    def load(cls, value: dict[str, Any], *, run: WorkflowRun) -> OutputCheckpoint:
        try:
            children = value.get("companions", [])
            if (
                not isinstance(children, list)
                or len(children) > 1
                or any(not isinstance(item, dict) or item.get("companions") for item in children)
            ):
                raise ValueError("saved output has invalid companions")
            result = cls(
                **{key: value[key] for key in cls.__dataclass_fields__ if key != "companions"},
                companions=tuple(cls.load(item, run=run) for item in children),
            )
        except (KeyError, TypeError) as exc:
            raise ValueError("saved output has no validated checkpoint identity") from exc
        if (
            result.version != (2 if result.companions else 1)
            or result.run_id != str(run.id)
            or result.project_id != str(run.project_id)
            or result.generation != run.generation
            or result.definition_commit_sha != run.definition_commit_sha
            or result.source_base_sha != run.expected_head_sha
            or any(
                not isinstance(sha, str) or _SHA.fullmatch(sha) is None
                for sha in (
                    result.definition_commit_sha,
                    result.source_base_sha,
                    result.ephemeral_commit_sha,
                )
            )
            or not isinstance(result.artifact_path, str)
            or not result.artifact_path
            or "\\" in result.artifact_path
            or any(part in {"", ".", ".."} for part in result.artifact_path.split("/"))
            or result.media_type
            not in {
                "text/markdown",
                "text/csv",
                "text/vnd.mermaid",
                "text/plain",
                "application/json",
                "image/svg+xml",
                "video/mp4",
            }
            or not isinstance(result.sha256, str)
            or _DIGEST.fullmatch(result.sha256) is None
            or type(result.byte_count) is not int
            or not 1 <= result.byte_count <= result.max_bytes
        ):
            raise ValueError("saved output does not belong to this run's validated checkpoint")
        if any(
            item.ephemeral_commit_sha != result.ephemeral_commit_sha
            or item.artifact_path == result.artifact_path
            or item.media_type != "text/markdown"
            for item in result.companions
        ):
            raise ValueError("saved companion does not belong to the same output revision")
        return result

    @property
    def binary(self) -> bool:
        return self.media_type in BINARY_MEDIA_TYPES

    @property
    def max_bytes(self) -> int:
        return MAX_BINARY_OUTPUT_BYTES if self.binary else MAX_TEXT_OUTPUT_BYTES

    def validate_content(self, content: bytes) -> None:
        if len(content) != self.byte_count or hashlib.sha256(content).hexdigest() != self.sha256:
            raise ValueError("saved output no longer matches its validated checkpoint")
        if not self.binary:
            content.decode("utf-8")


@dataclass(frozen=True)
class RunOutput:
    path: str
    revision: str
    content: bytes


async def related_output_documents(database, run: WorkflowRun) -> list[dict[str, str]]:
    """Small Postgres-backed file links, never another approval or artifact read."""
    if not run.canonical_commit_sha or not run.artifact_path or run.executor != "codex.procedure":
        return []
    receipt = await database.get_effect(f"{run.id}:procedure_canonical_commit")
    data = (
        (receipt.result or {}).get("checkpoint")
        if receipt and receipt.status == "completed"
        else None
    )
    if not data:
        return []
    checkpoint = OutputCheckpoint.load(data, run=run)
    if checkpoint.artifact_path != run.artifact_path:
        return []
    return [
        {
            "label": "Generation notes"
            if item.artifact_path.endswith(".generation.md")
            else item.artifact_path.rsplit("/", 1)[-1]
            .removesuffix(".md")
            .replace("_", " ")
            .title(),
            "path": item.artifact_path,
            "revision": run.canonical_commit_sha,
            "url": f"/file?project={run.project_id}&"
            + urlencode(
                {
                    "path": item.artifact_path,
                    "revision": run.canonical_commit_sha,
                }
            ),
        }
        for item in checkpoint.companions
    ]


def retained_output_view(run: WorkflowRun) -> dict[str, Any] | None:
    if run.retained_output is None:
        return None
    checkpoint = OutputCheckpoint.load(run.retained_output, run=run)
    reason = run.retained_output.get("reason")
    if reason not in OUTPUT_REASONS:
        raise ValueError("saved output has an invalid availability reason")
    return {
        "artifact_path": checkpoint.artifact_path,
        "revision": checkpoint.ephemeral_commit_sha,
        "media_type": checkpoint.media_type,
        "byte_count": checkpoint.byte_count,
        "reason": reason,
    }


async def read_run_output(
    *,
    storage: CodeStorage,
    run: WorkflowRun,
    repo_id: str,
    source: Literal["canonical", "retained"] = "canonical",
) -> RunOutput:
    """Read one projected output. HTTP/MCP callers must authorize the run first."""
    if source == "retained":
        if run.executor not in RETAINED_OUTPUT_EXECUTORS or retained_output_view(run) is None:
            raise ValueError("generated output is not available")
        checkpoint = OutputCheckpoint.load(run.retained_output, run=run)
        content = await storage.read_procedure_checkpoint(
            repo_id=repo_id,
            revision=checkpoint.ephemeral_commit_sha,
            path=checkpoint.artifact_path,
            binary=checkpoint.media_type == "video/mp4",
        )
        checkpoint.validate_content(content)
        return RunOutput(checkpoint.artifact_path, checkpoint.ephemeral_commit_sha, content)
    if source != "canonical" or run.canonical_commit_sha is None or run.artifact_path is None:
        raise ValueError("canonical output is not available")
    content = await storage.read_canonical_artifact(
        repo_id=repo_id,
        commit_sha=run.canonical_commit_sha,
        path=run.artifact_path,
    )
    return RunOutput(run.artifact_path, run.canonical_commit_sha, content)
