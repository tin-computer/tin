"""Declarative workflow prerequisites: what a project must already hold before a run is admitted.

A prerequisite is a claim about project state, never a log entry. Satisfaction is derived on
demand from succeeded pinned runs, the project-state HEAD and active test identities, so it
cannot drift. The declaration lives in the immutable workflow definition next to
``integration_requirements`` and is evaluated once at the run admission choke point.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import UUID

from tin_lite.domain import (
    ANSWER_PAGE_WORKFLOW_NAME,
    CODEX_PROCEDURE_EXECUTOR,
    MEMORY_INDEX_PATH,
    PREREQUISITE_WAIT_MINUTES,
)
from tin_lite.procedures import memory_section_present
from tin_lite.project_files import safe_project_file_path

if TYPE_CHECKING:
    from tin_lite.domain import Workflow, WorkflowRun

LEVELS = ("required", "recommended")
KINDS = ("run", "artifact", "identity")
SCOPES = ("product_host", "site_origin", "market")
IDENTITY_REUSE = "active"
PREREQUISITE_MISSING = "prerequisite_missing"
EVIDENCE_VERSION = 1
MAX_REASON_CHARS = 200
MAX_AGE_DAYS = 3650
_WORKFLOW_KEY = re.compile(r"^[a-z][a-z0-9]*\.[a-z][a-z0-9_]*$")
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_RUN_KEYS = frozenset({"kind", "workflow", "level", "reason", "match", "via_input", "max_age_days"})
_ARTIFACT_KEYS = frozenset({"kind", "path", "level", "reason", "section", "producer"})
_IDENTITY_KEYS = frozenset({"kind", "reuse", "host_input", "level", "reason", "producer"})
_ACTIVE_IDENTITY = ("active", "password")
# Executors whose Temporal workflow holds a newly admitted run, with no compute, until the
# prerequisite runs it was admitted behind finish, and which read project state only after.
WAITING_EXECUTORS = frozenset({ANSWER_PAGE_WORKFLOW_NAME, CODEX_PROCEDURE_EXECUTOR})
# These pin their sources at admission, so a result that lands later could not reach them.
PINNED_SOURCE_WORKFLOWS = frozenset({"content.generate", "brand.capture"})


def can_wait_for_prerequisites(workflow: Workflow) -> bool:
    """Whether a run of this workflow can wait for a prerequisite run that is still going.

    workflow.code pins its project files at admission and the other native executors have
    no wait step, so for them a running prerequisite is reported, not waited for.
    """
    return workflow.executor in WAITING_EXECUTORS and workflow.key not in PINNED_SOURCE_WORKFLOWS


@dataclass(frozen=True)
class WorkflowPrerequisite:
    """One immutable dependency of a workflow on earlier project work."""

    kind: str
    level: str
    reason: str
    workflow: str | None = None
    match: tuple[str, ...] = ()
    via_input: str | None = None
    max_age_days: int | None = None
    path: str | None = None
    section: str | None = None
    reuse: str | None = None
    host_input: str | None = None
    producer: str | None = None

    @property
    def upstream(self) -> str | None:
        """The workflow key an agent should run to satisfy this prerequisite, if any."""
        return self.workflow if self.kind == "run" else self.producer

    @property
    def required(self) -> bool:
        return self.level == "required"

    def placeholders(self) -> tuple[str, ...]:
        return tuple(_PLACEHOLDER.findall(self.path or ""))

    def definition(self) -> dict[str, Any]:
        item: dict[str, Any] = {"kind": self.kind, "level": self.level, "reason": self.reason}
        if self.kind == "run":
            item["workflow"] = self.workflow
            if self.match:
                item["match"] = list(self.match)
            if self.via_input is not None:
                item["via_input"] = self.via_input
            if self.max_age_days is not None:
                item["max_age_days"] = self.max_age_days
        elif self.kind == "artifact":
            item["path"] = self.path
            if self.section is not None:
                item["section"] = self.section
            if self.producer is not None:
                item["producer"] = self.producer
        else:
            item["reuse"] = self.reuse
            item["host_input"] = self.host_input
            if self.producer is not None:
                item["producer"] = self.producer
        return item

    def identity(self) -> tuple[str, ...]:
        if self.kind == "run":
            return ("run", self.workflow or "")
        if self.kind == "artifact":
            return ("artifact", self.path or "", self.section or "")
        return ("identity", self.host_input or "")


class PrerequisiteError(RuntimeError):
    """A required prerequisite is unmet; only Tin-owned text crosses the HTTP/MCP boundary."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 409,
        prerequisites: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.code, self.status = code, status
        self.prerequisites = prerequisites or []

    def diagnostic(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "prerequisites": self.prerequisites}

    @classmethod
    def from_evaluation(
        cls,
        evaluation: PrerequisiteEvaluation,
        *,
        workflow: Workflow,
        inputs: Mapping[str, Any],
    ) -> PrerequisiteError:
        blocking = evaluation.blocking
        count = len(blocking)
        steps = "; ".join(result.how_to_satisfy for result in blocking)
        message = (
            f"{workflow.title} ({workflow.key}) needs {count} prerequisite"
            f"{'' if count == 1 else 's'} first: {steps}"
        )
        return cls(PREREQUISITE_MISSING, message, prerequisites=evaluation.views(inputs=inputs))


@dataclass(frozen=True)
class PrerequisiteResult:
    prerequisite: WorkflowPrerequisite
    satisfied: bool
    skipped: bool = False
    evidence: dict[str, Any] | None = None
    how_to_satisfy: str = ""
    upstream: Workflow | None = None
    # A pending or running upstream run that could still satisfy this prerequisite.
    running_run_id: str | None = None

    @property
    def unmet(self) -> bool:
        return not self.satisfied and not self.skipped


@dataclass(frozen=True)
class PrerequisiteEvaluation:
    results: tuple[PrerequisiteResult, ...]
    head_commit_sha: str | None = None
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # Whether the admitted run waits for running upstream runs instead of going without them.
    can_wait: bool = False

    def _waits(self, result: PrerequisiteResult) -> bool:
        return self.can_wait and result.unmet and result.running_run_id is not None

    @property
    def waiting(self) -> tuple[PrerequisiteResult, ...]:
        """Unmet prerequisites whose upstream run is still going; the new run waits for them."""
        return tuple(r for r in self.results if self._waits(r))

    @property
    def blocking(self) -> tuple[PrerequisiteResult, ...]:
        return tuple(
            r for r in self.results if r.unmet and r.prerequisite.required and not self._waits(r)
        )

    @property
    def advisories(self) -> tuple[PrerequisiteResult, ...]:
        return tuple(
            r
            for r in self.results
            if r.unmet and not r.prerequisite.required and not self._waits(r)
        )

    def views(
        self, *, inputs: Mapping[str, Any], results: Sequence[PrerequisiteResult] | None = None
    ) -> list[dict[str, Any]]:
        return [
            _view(result, inputs=inputs, can_wait=self.can_wait)
            for result in (self.results if results is None else results)
        ]

    def evidence(self, *, inputs: Mapping[str, Any]) -> dict[str, Any]:
        """The bounded record pinned on the admitted run: what the gate resolved, and when."""
        items = []
        for result in self.results:
            item = {
                **_subject(result.prerequisite),
                "level": result.prerequisite.level,
                "satisfied": result.satisfied,
            }
            if result.skipped:
                item["skipped"] = True
            if result.evidence:
                item["evidence"] = result.evidence
            if result.running_run_id is not None and not result.satisfied:
                item["running_run_id"] = result.running_run_id
            items.append(item)
        advisories = self.views(inputs=inputs, results=self.advisories)
        waiting = self.views(inputs=inputs, results=self.waiting)
        evidence: dict[str, Any] = {
            "version": EVIDENCE_VERSION,
            "evaluated_at": self.evaluated_at.isoformat(),
            "head_commit_sha": self.head_commit_sha,
            "items": items,
            "advisories": advisories,
        }
        if waiting:
            evidence["waiting"] = waiting
            evidence["wait_limit_minutes"] = PREREQUISITE_WAIT_MINUTES
        notes = [view["note"] for view in waiting + advisories if view.get("note")]
        if notes:
            evidence["notes"] = notes
        return evidence


def parse_workflow_prerequisites(
    value: Any, *, input_schema: Mapping[str, Any] | None = None
) -> tuple[WorkflowPrerequisite, ...]:
    """Validate the deliberately small prerequisite contract stored in a workflow definition."""

    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("workflow prerequisites must be a list")
    if len(value) > 16:
        raise ValueError("workflow prerequisites are limited to 16 entries")
    inputs = _schema_inputs(input_schema)
    parsed: list[WorkflowPrerequisite] = []
    seen: set[tuple[str, ...]] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("workflow prerequisite must be an object")
        kind = item.get("kind")
        if kind not in KINDS:
            raise ValueError("workflow prerequisite kind must be run, artifact or identity")
        allowed = {"run": _RUN_KEYS, "artifact": _ARTIFACT_KEYS, "identity": _IDENTITY_KEYS}[kind]
        if set(item) - allowed:
            raise ValueError(f"workflow {kind} prerequisite has unsupported fields")
        level = item.get("level")
        if level not in LEVELS:
            raise ValueError("workflow prerequisite level must be required or recommended")
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_REASON_CHARS:
            raise ValueError(
                f"workflow prerequisite reason must be 1-{MAX_REASON_CHARS} characters"
            )
        if kind == "run":
            prerequisite = _parse_run(item, level=level, reason=reason, inputs=inputs)
        elif kind == "artifact":
            prerequisite = _parse_artifact(item, level=level, reason=reason, inputs=inputs)
        else:
            prerequisite = _parse_identity(item, level=level, reason=reason, inputs=inputs)
        if prerequisite.identity() in seen:
            raise ValueError("workflow prerequisites contain a duplicate entry")
        seen.add(prerequisite.identity())
        parsed.append(prerequisite)
    return tuple(parsed)


def validate_prerequisite_graph(
    definitions: Mapping[str, Sequence[WorkflowPrerequisite]],
) -> None:
    """Every referenced workflow must exist and the catalog must contain no dependency cycle."""

    edges: dict[str, list[str]] = {}
    for key, prerequisites in definitions.items():
        targets = []
        for prerequisite in prerequisites:
            for target in (prerequisite.workflow, prerequisite.producer):
                if target is None:
                    continue
                if target == key:
                    raise ValueError(f"workflow {key} lists itself as a prerequisite")
                if target not in definitions:
                    raise ValueError(f"workflow {key} requires unknown workflow {target}")
                targets.append(target)
        edges[key] = targets
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> None:
        marker = state.get(node, 0)
        if marker == 2:
            return
        if marker == 1:
            cycle = stack[stack.index(node) :] + [node]
            raise ValueError("workflow prerequisites form a cycle: " + " -> ".join(cycle))
        state[node] = 1
        stack.append(node)
        for target in edges[node]:
            visit(target)
        stack.pop()
        state[node] = 2

    for key in sorted(edges):
        visit(key)


def workflow_prerequisites(workflow: Workflow) -> tuple[WorkflowPrerequisite, ...]:
    return parse_workflow_prerequisites(
        workflow.definition.get("prerequisites"),
        input_schema=workflow.definition.get("input_schema"),
    )


async def evaluate_prerequisites(
    *,
    database: Any,
    storage: Any,
    project_id: UUID,
    workflow: Workflow,
    normalized_inputs: Mapping[str, Any],
    workflows_by_key: Mapping[str, Workflow] | None = None,
    now: datetime | None = None,
    can_wait: bool = False,
) -> PrerequisiteEvaluation:
    """Decide, from durable project facts, whether this exact start may be admitted.

    Bounded work: one catalog listing (unless supplied), one succeeded-run query covering every
    run prerequisite, one identity lookup per identity prerequisite, one HEAD file listing, at
    most one read per sectioned path and, only when something is unmet, one query for upstream
    runs still in progress. With can_wait, an unmet prerequisite whose upstream run is still
    going does not block: the admitted run waits for that run before it does any work.
    """

    prerequisites = workflow_prerequisites(workflow)
    if not prerequisites:
        return PrerequisiteEvaluation(results=(), can_wait=can_wait)
    now = now or datetime.now(UTC)
    catalog = workflows_by_key
    if catalog is None:
        catalog = {item.key: item for item in await database.list_workflows(project_id=project_id)}
    run_keys = sorted({p.workflow for p in prerequisites if p.kind == "run" and p.workflow})
    candidates: dict[str, list[WorkflowRun]] = {key: [] for key in run_keys}
    if run_keys:
        for key, run in await database.list_prerequisite_runs(
            project_id=project_id, workflow_keys=run_keys
        ):
            candidates.setdefault(key, []).append(run)
    head: str | None = None
    paths: set[str] | None = None
    section_texts: dict[str, str | None] = {}
    artifact_items = [
        (p, _resolve_path(p, normalized_inputs)) for p in prerequisites if p.kind == "artifact"
    ]
    if any(resolved is not None for _, resolved in artifact_items):
        if storage is None or not hasattr(storage, "list_canonical_files"):
            if any(p.required and resolved is not None for p, resolved in artifact_items):
                raise RuntimeError("project state storage is unavailable")
            paths = set()
        else:
            project = await database.get_project(project_id)
            if project is None:
                raise LookupError("project not found")
            listed, head = await _head_files(storage, project)
            paths = set(listed)
            for prerequisite, resolved in artifact_items:
                if (
                    prerequisite.section is not None
                    and resolved in paths
                    and resolved not in section_texts
                    and head is not None
                ):
                    raw = await storage.read_canonical_artifact(
                        repo_id=project.state_repo_id, commit_sha=head, path=resolved
                    )
                    section_texts[resolved] = raw.decode("utf-8", errors="replace")
    results: list[PrerequisiteResult] = []
    for prerequisite in prerequisites:
        upstream = catalog.get(prerequisite.upstream) if prerequisite.upstream else None
        if prerequisite.kind == "run":
            results.append(
                _evaluate_run(
                    prerequisite,
                    inputs=normalized_inputs,
                    candidates=candidates.get(prerequisite.workflow or "", []),
                    now=now,
                    upstream=upstream,
                )
            )
        elif prerequisite.kind == "identity":
            results.append(
                await _evaluate_identity(
                    prerequisite,
                    database=database,
                    project_id=project_id,
                    inputs=normalized_inputs,
                    upstream=upstream,
                )
            )
        else:
            resolved = next(r for p, r in artifact_items if p is prerequisite)
            results.append(
                _evaluate_artifact(
                    prerequisite,
                    resolved=resolved,
                    paths=paths if paths is not None else set(),
                    head=head,
                    section_texts=section_texts,
                    upstream=upstream,
                )
            )
    results = await _with_running_upstream(
        results, database=database, project_id=project_id, inputs=normalized_inputs
    )
    if can_wait:
        results = [
            replace(result, how_to_satisfy=_wait_hint(result))
            if result.unmet and result.running_run_id is not None
            else result
            for result in results
        ]
    return PrerequisiteEvaluation(
        results=tuple(results), head_commit_sha=head, evaluated_at=now, can_wait=can_wait
    )


async def _with_running_upstream(
    results: list[PrerequisiteResult],
    *,
    database: Any,
    project_id: UUID,
    inputs: Mapping[str, Any],
) -> list[PrerequisiteResult]:
    """Attach the newest pending or running upstream run that could satisfy each unmet item."""
    keys = sorted({r.prerequisite.upstream for r in results if r.unmet and r.prerequisite.upstream})
    if not keys:
        return results
    active: dict[str, list[WorkflowRun]] = {}
    for key, run in await database.list_active_prerequisite_runs(
        project_id=project_id, workflow_keys=keys
    ):
        active.setdefault(key, []).append(run)
    attached = []
    for result in results:
        upstream = result.prerequisite.upstream
        running = None
        if result.unmet and upstream in active:
            running = _running_match(result.prerequisite, active[upstream], inputs=inputs)
        if running is None:
            attached.append(result)
            continue
        attached.append(
            replace(
                result,
                running_run_id=str(running.id),
                how_to_satisfy=(
                    f"{upstream} run {running.id} is still running; call get_run with run_id "
                    f"{running.id} until it succeeds, then start this again."
                ),
            )
        )
    return attached


def _running_match(
    prerequisite: WorkflowPrerequisite,
    runs: Sequence[WorkflowRun],
    *,
    inputs: Mapping[str, Any],
) -> WorkflowRun | None:
    """The in-progress run that would satisfy this prerequisite if it succeeds, newest first."""
    if prerequisite.kind == "run" and prerequisite.via_input is not None:
        wanted = str(inputs.get(prerequisite.via_input) or "")
        return next((run for run in runs if str(run.id) == wanted), None)
    if prerequisite.kind == "run" and prerequisite.match:
        scope = scope_signature(inputs, prerequisite.match)
        if scope is None:
            return None
        return next(
            (run for run in runs if scope_signature(run.input or {}, prerequisite.match) == scope),
            None,
        )
    if prerequisite.kind == "identity":
        host = _host_of(inputs.get(prerequisite.host_input or ""))
        if host is None:
            return None
        return next(
            (
                run
                for run in runs
                if _host_of((run.input or {}).get(prerequisite.host_input or "")) == host
            ),
            None,
        )
    return runs[0] if runs else None


def prerequisite_wait_run_ids(evidence: Any) -> list[str]:
    """The upstream run ids an admitted run still waits for; empty once the wait is settled."""
    if not isinstance(evidence, Mapping) or evidence.get("waited") is not None:
        return []
    waiting = evidence.get("waiting")
    if not isinstance(waiting, list):
        return []
    ids = [
        str(view["running_run_id"])
        for view in waiting
        if isinstance(view, Mapping) and view.get("running_run_id")
    ]
    return list(dict.fromkeys(ids))  # A run and its identity can wait on the same run.


def _wait_hint(result: PrerequisiteResult) -> str:
    return (
        f"Tin waits up to {PREREQUISITE_WAIT_MINUTES} minutes for "
        f"{result.prerequisite.upstream} run {result.running_run_id} to finish, "
        "then reads its result."
    )


async def project_readiness(
    *,
    database: Any,
    storage: Any,
    project_id: UUID,
    workflows: Sequence[Workflow],
) -> dict[UUID, dict[str, Any]]:
    """Input-free readiness for listings: at most four backend calls for any catalog size.

    A run prerequisite counts as met when any succeeded pinned run of that workflow exists in
    the project; an identity when any active test account exists; an artifact with input
    placeholders is assumed met. Exact scopes are only checked against real inputs at start.
    """

    parsed = {item.id: workflow_prerequisites(item) for item in workflows}
    catalog = {item.key: item for item in workflows}
    all_items = [p for items in parsed.values() for p in items]
    if not all_items:
        return {item.id: _readiness(()) for item in workflows}
    run_keys = sorted({p.workflow for p in all_items if p.kind == "run" and p.workflow})
    succeeded: set[str] = set()
    if run_keys:
        succeeded = {
            key
            for key, _run in await database.list_prerequisite_runs(
                project_id=project_id, workflow_keys=run_keys
            )
        }
    active_hosts: set[str] | None = None
    if any(p.kind == "identity" for p in all_items):
        identities = await database.list_test_identities(project_id=project_id)
        active_hosts = {
            identity.target_host
            for identity in identities
            if (identity.status, identity.auth_kind) == _ACTIVE_IDENTITY
            and identity.password_ciphertext is not None
        }
    paths: set[str] = set()
    head: str | None = None
    section_texts: dict[str, str | None] = {}
    exact_paths = {p.path for p in all_items if p.kind == "artifact" and not p.placeholders()}
    if exact_paths and storage is not None and hasattr(storage, "list_canonical_files"):
        project = await database.get_project(project_id)
        if project is not None:
            listed, head = await _head_files(storage, project)
            paths = set(listed)
            for path in sorted(
                {
                    p.path
                    for p in all_items
                    if p.kind == "artifact" and p.section is not None and p.path in paths
                }
            ):
                if head is None or path is None:
                    continue
                raw = await storage.read_canonical_artifact(
                    repo_id=project.state_repo_id, commit_sha=head, path=path
                )
                section_texts[path] = raw.decode("utf-8", errors="replace")
    readiness: dict[UUID, dict[str, Any]] = {}
    for item in workflows:
        results = []
        for prerequisite in parsed[item.id]:
            upstream = catalog.get(prerequisite.upstream) if prerequisite.upstream else None
            if prerequisite.kind == "run":
                satisfied = prerequisite.workflow in succeeded
                how = _run_hint(prerequisite, inputs={})
                scoped = bool(prerequisite.match or prerequisite.via_input)
            elif prerequisite.kind == "identity":
                satisfied = bool(active_hosts)
                how = _identity_hint(prerequisite, host=None)
                scoped = True
            elif prerequisite.placeholders():
                satisfied, how, scoped = True, "", True
            else:
                satisfied = _artifact_present(prerequisite, prerequisite.path, paths, section_texts)
                how = _artifact_hint(prerequisite, resolved=prerequisite.path)
                scoped = False
            results.append(
                PrerequisiteResult(
                    prerequisite=prerequisite,
                    satisfied=satisfied,
                    how_to_satisfy=how,
                    upstream=upstream,
                    evidence={"scope": "checked_at_start"} if scoped else None,
                )
            )
        readiness[item.id] = _readiness(tuple(results))
    return readiness


def _readiness(results: tuple[PrerequisiteResult, ...]) -> dict[str, Any]:
    unmet = [r for r in results if r.unmet]
    if any(r.prerequisite.required for r in unmet):
        state = "blocked"
    elif unmet:
        state = "advisory"
    else:
        state = "ready"
    return {
        "state": state,
        "unmet": [_view(result, inputs={}) for result in unmet],
        "note": (
            "Run scopes, via_input ids and {placeholder} paths are checked against the actual "
            "inputs at start."
        ),
    }


def _schema_inputs(input_schema: Mapping[str, Any] | None) -> set[str] | None:
    if input_schema is None:
        return None
    properties = input_schema.get("properties")
    if not isinstance(properties, Mapping):
        return set()
    return {name for name in properties if name != "project_id"}


def _check_input_name(name: Any, *, field_name: str, inputs: set[str] | None) -> str:
    if not isinstance(name, str) or not name or name == "project_id":
        raise ValueError(f"workflow prerequisite {field_name} must name a workflow input")
    if inputs is not None and name not in inputs:
        raise ValueError(f"workflow prerequisite {field_name} names unknown input {name}")
    return name


def _check_key(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not _WORKFLOW_KEY.fullmatch(value):
        raise ValueError(f"workflow prerequisite {field_name} must be a workflow key")
    return value


def _parse_run(item, *, level, reason, inputs) -> WorkflowPrerequisite:
    workflow = _check_key(item.get("workflow"), field_name="workflow")
    match_raw = item.get("match")
    match: tuple[str, ...] = ()
    if match_raw is not None:
        if (
            not isinstance(match_raw, list)
            or not match_raw
            or any(scope not in SCOPES for scope in match_raw)
            or len(set(match_raw)) != len(match_raw)
        ):
            raise ValueError("workflow prerequisite match must list unique known scopes")
        match = tuple(match_raw)
    via_input = item.get("via_input")
    if via_input is not None:
        via_input = _check_input_name(via_input, field_name="via_input", inputs=inputs)
        if match:
            raise ValueError("workflow prerequisite via_input cannot be combined with match")
    max_age_days = item.get("max_age_days")
    if max_age_days is not None and (
        type(max_age_days) is not int or not 1 <= max_age_days <= MAX_AGE_DAYS
    ):
        raise ValueError(f"workflow prerequisite max_age_days must be 1-{MAX_AGE_DAYS}")
    return WorkflowPrerequisite(
        kind="run",
        level=level,
        reason=reason,
        workflow=workflow,
        match=match,
        via_input=via_input,
        max_age_days=max_age_days,
    )


def _parse_artifact(item, *, level, reason, inputs) -> WorkflowPrerequisite:
    path = item.get("path")
    if not isinstance(path, str) or not path:
        raise ValueError("workflow prerequisite path is missing")
    placeholders = _PLACEHOLDER.findall(path)
    for name in placeholders:
        _check_input_name(name, field_name="path placeholder", inputs=inputs)
    probe = _PLACEHOLDER.sub("x", path)
    if "{" in probe or "}" in probe or not safe_project_file_path(probe):
        raise ValueError("workflow prerequisite path is unsafe")
    section = item.get("section")
    if section is not None:
        if path != MEMORY_INDEX_PATH:
            raise ValueError(f"workflow prerequisite section requires path {MEMORY_INDEX_PATH}")
        if not isinstance(section, str) or not section.startswith("### ") or len(section) > 80:
            raise ValueError("workflow prerequisite section must be a ### heading")
    producer = item.get("producer")
    if producer is not None:
        producer = _check_key(producer, field_name="producer")
    return WorkflowPrerequisite(
        kind="artifact",
        level=level,
        reason=reason,
        path=path,
        section=section,
        producer=producer,
    )


def _parse_identity(item, *, level, reason, inputs) -> WorkflowPrerequisite:
    if item.get("reuse") != IDENTITY_REUSE:
        raise ValueError("workflow prerequisite identity reuse must be active")
    host_input = _check_input_name(item.get("host_input"), field_name="host_input", inputs=inputs)
    producer = item.get("producer")
    if producer is not None:
        producer = _check_key(producer, field_name="producer")
    return WorkflowPrerequisite(
        kind="identity",
        level=level,
        reason=reason,
        reuse=IDENTITY_REUSE,
        host_input=host_input,
        producer=producer,
    )


def _host_of(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        host = (urlsplit(value.strip()).hostname or "").lower()
    except ValueError:
        return None
    return host or None


def _product_host(inputs: Mapping[str, Any]) -> str | None:
    return _host_of(inputs.get("product_url"))


def _site_origin(inputs: Mapping[str, Any]) -> str | None:
    return _host_of(inputs.get("site_url"))


def _market(inputs: Mapping[str, Any]) -> str | None:
    value = inputs.get("market")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().lower()


SCOPE_EXTRACTORS = {"product_host": _product_host, "site_origin": _site_origin, "market": _market}


def scope_signature(inputs: Mapping[str, Any], match: Sequence[str]) -> tuple[str, ...] | None:
    values = []
    for scope in match:
        value = SCOPE_EXTRACTORS[scope](inputs or {})
        if value is None:
            return None
        values.append(value)
    return tuple(values)


def _evaluate_run(
    prerequisite: WorkflowPrerequisite,
    *,
    inputs: Mapping[str, Any],
    candidates: Sequence[WorkflowRun],
    now: datetime,
    upstream: Workflow | None,
) -> PrerequisiteResult:
    hint = _run_hint(prerequisite, inputs=inputs)
    selected: WorkflowRun | None = None
    if prerequisite.via_input is not None:
        raw = inputs.get(prerequisite.via_input)
        try:
            wanted = UUID(str(raw)) if raw not in (None, "") else None
        except ValueError:
            wanted = None
        if wanted is None:
            return PrerequisiteResult(
                prerequisite=prerequisite,
                satisfied=False,
                how_to_satisfy=(
                    f"Set {prerequisite.via_input} to the id of a successful "
                    f"{prerequisite.workflow} run in this project."
                ),
                upstream=upstream,
            )
        selected = next((run for run in candidates if run.id == wanted), None)
    else:
        wanted_scope = scope_signature(inputs, prerequisite.match)
        if prerequisite.match and wanted_scope is None:
            return PrerequisiteResult(
                prerequisite=prerequisite,
                satisfied=False,
                how_to_satisfy=(
                    "Provide "
                    + ", ".join(_SCOPE_INPUTS[scope] for scope in prerequisite.match)
                    + f" so Tin can match an earlier {prerequisite.workflow} run."
                ),
                upstream=upstream,
            )
        for run in candidates:
            if prerequisite.match and scope_signature(run.input or {}, prerequisite.match) != (
                wanted_scope
            ):
                continue
            selected = run
            break
    if selected is not None and prerequisite.max_age_days is not None:
        finished = selected.finished_at or selected.created_at
        if finished is None or now - finished > timedelta(days=prerequisite.max_age_days):
            return PrerequisiteResult(
                prerequisite=prerequisite,
                satisfied=False,
                how_to_satisfy=(
                    f"The newest matching {prerequisite.workflow} run is older than "
                    f"{prerequisite.max_age_days} days; run it again."
                ),
                upstream=upstream,
            )
    if selected is None:
        return PrerequisiteResult(
            prerequisite=prerequisite, satisfied=False, how_to_satisfy=hint, upstream=upstream
        )
    finished = selected.finished_at or selected.created_at
    return PrerequisiteResult(
        prerequisite=prerequisite,
        satisfied=True,
        evidence={
            "run_id": str(selected.id),
            "workflow": prerequisite.workflow,
            "canonical_commit_sha": selected.canonical_commit_sha,
            "finished_at": finished.isoformat() if finished else None,
        },
        upstream=upstream,
    )


async def _evaluate_identity(
    prerequisite: WorkflowPrerequisite,
    *,
    database: Any,
    project_id: UUID,
    inputs: Mapping[str, Any],
    upstream: Workflow | None,
) -> PrerequisiteResult:
    host = _host_of(inputs.get(prerequisite.host_input or ""))
    if host is None:
        return PrerequisiteResult(
            prerequisite=prerequisite,
            satisfied=False,
            how_to_satisfy=f"Provide {prerequisite.host_input} so Tin can find its test account.",
            upstream=upstream,
        )
    identity = await database.find_reusable_test_identity(project_id=project_id, target_host=host)
    if identity is None:
        return PrerequisiteResult(
            prerequisite=prerequisite,
            satisfied=False,
            how_to_satisfy=_identity_hint(prerequisite, host=host),
            upstream=upstream,
        )
    return PrerequisiteResult(
        prerequisite=prerequisite,
        satisfied=True,
        evidence={
            "identity_id": str(identity.id),
            "host": host,
            "created_by_run_id": str(identity.created_by_run_id),
        },
        upstream=upstream,
    )


def _resolve_path(prerequisite: WorkflowPrerequisite, inputs: Mapping[str, Any]) -> str | None:
    """The concrete path for this start, or None when a placeholder input is empty."""
    path = prerequisite.path or ""
    for name in prerequisite.placeholders():
        value = inputs.get(name)
        if not isinstance(value, str) or not value.strip():
            return None
        path = path.replace("{" + name + "}", value.strip())
    return path if safe_project_file_path(path) else None


def _artifact_present(
    prerequisite: WorkflowPrerequisite,
    resolved: str | None,
    paths: set[str],
    section_texts: Mapping[str, str | None],
) -> bool:
    if resolved is None or resolved not in paths:
        return False
    if prerequisite.section is None:
        return True
    text = section_texts.get(resolved)
    return text is not None and memory_section_present(text, prerequisite.section)


def _evaluate_artifact(
    prerequisite: WorkflowPrerequisite,
    *,
    resolved: str | None,
    paths: set[str],
    head: str | None,
    section_texts: Mapping[str, str | None],
    upstream: Workflow | None,
) -> PrerequisiteResult:
    if resolved is None and prerequisite.placeholders():
        return PrerequisiteResult(prerequisite=prerequisite, satisfied=True, skipped=True)
    if resolved is None:
        return PrerequisiteResult(
            prerequisite=prerequisite,
            satisfied=False,
            how_to_satisfy="The prerequisite path resolved to an unsafe project path.",
            upstream=upstream,
        )
    if not _artifact_present(prerequisite, resolved, paths, section_texts):
        return PrerequisiteResult(
            prerequisite=prerequisite,
            satisfied=False,
            how_to_satisfy=_artifact_hint(prerequisite, resolved=resolved),
            upstream=upstream,
        )
    evidence: dict[str, Any] = {"path": resolved, "revision": head}
    if prerequisite.section is not None:
        evidence["section"] = prerequisite.section
    return PrerequisiteResult(
        prerequisite=prerequisite, satisfied=True, evidence=evidence, upstream=upstream
    )


async def _head_files(storage: Any, project: Any) -> tuple[list[str], str | None]:
    try:
        return await storage.list_canonical_files(
            repo_id=project.state_repo_id, branch=project.canonical_branch
        )
    except RuntimeError as exc:
        if "no canonical head" in str(exc):
            return [], None
        raise


_SCOPE_INPUTS = {"product_host": "product_url", "site_origin": "site_url", "market": "market"}


def _run_hint(prerequisite: WorkflowPrerequisite, *, inputs: Mapping[str, Any]) -> str:
    scope = ""
    if prerequisite.match:
        parts = [
            str(inputs.get(_SCOPE_INPUTS[name]))
            for name in prerequisite.match
            if inputs.get(_SCOPE_INPUTS[name])
        ]
        if parts:
            scope = " for " + " / ".join(parts)
    return f"Run {prerequisite.workflow}{scope} and wait for it to succeed."


def _identity_hint(prerequisite: WorkflowPrerequisite, *, host: str | None) -> str:
    producer = prerequisite.producer or "the signup walkthrough"
    where = f" on {host}" if host else ""
    return f"Run {producer}{where} and wait until its test account is active."


def _artifact_hint(prerequisite: WorkflowPrerequisite, *, resolved: str | None) -> str:
    target = resolved or prerequisite.path or ""
    if prerequisite.section is not None:
        what = f"{target} gains a {prerequisite.section} section"
    else:
        what = f"{target} exists"
    if prerequisite.producer:
        return f"Run {prerequisite.producer} so {what}."
    return f"Commit {target} with commit_project_changes so {what}."


def _subject(prerequisite: WorkflowPrerequisite) -> dict[str, Any]:
    subject: dict[str, Any] = {"kind": prerequisite.kind}
    if prerequisite.kind == "run":
        subject["workflow"] = prerequisite.workflow
    elif prerequisite.kind == "artifact":
        subject["path"] = prerequisite.path
        if prerequisite.section is not None:
            subject["section"] = prerequisite.section
    else:
        subject["host_input"] = prerequisite.host_input
    return subject


def _view(
    result: PrerequisiteResult, *, inputs: Mapping[str, Any], can_wait: bool | None = None
) -> dict[str, Any]:
    """One prerequisite as HTTP/MCP show it; admission views (can_wait set) add a plain note."""
    prerequisite = result.prerequisite
    view: dict[str, Any] = {
        **_subject(prerequisite),
        "level": prerequisite.level,
        "satisfied": result.satisfied,
        "reason": prerequisite.reason,
        "workflow_key": prerequisite.upstream,
        "workflow_id": str(result.upstream.id) if result.upstream else None,
        "title": result.upstream.title if result.upstream else None,
    }
    if result.skipped:
        view["skipped"] = True
    if result.evidence and result.evidence.get("scope") == "checked_at_start":
        view["scope"] = "checked_at_start"
    if not result.satisfied:
        view["how_to_satisfy"] = result.how_to_satisfy
        if result.running_run_id is not None:
            view["running_run_id"] = result.running_run_id
            view["suggested_call"] = {"tool": "get_run", "run_id": result.running_run_id}
        elif result.upstream is not None:
            view["suggested_call"] = _suggested_call(result.upstream, inputs=inputs)
        note = _note(result, can_wait=can_wait) if can_wait is not None else None
        if note:
            view["note"] = note
    return view


def _missing_subject(prerequisite: WorkflowPrerequisite) -> str:
    if prerequisite.kind == "run":
        return f"a successful {prerequisite.workflow} run"
    if prerequisite.kind == "artifact":
        if prerequisite.section is not None:
            return f"the {prerequisite.section} section of {prerequisite.path}"
        return prerequisite.path or "a project file"
    return "an active test account"


def _note(result: PrerequisiteResult, *, can_wait: bool) -> str | None:
    """What happens to the admitted run because of this unmet prerequisite, in plain words."""
    if not result.unmet:
        return None
    prerequisite, running = result.prerequisite, result.running_run_id
    if running is not None and can_wait:
        return (
            f"This run waits up to {PREREQUISITE_WAIT_MINUTES} minutes for "
            f"{prerequisite.upstream} run {running} to finish, then reads its result."
        )
    if prerequisite.required:
        return None  # The start is refused; the error says what to run first.
    subject = _missing_subject(prerequisite)
    if running is not None:
        return (
            f"Tin starts this run without {subject}: {prerequisite.upstream} run {running} "
            "is still running, and this workflow cannot wait for it."
        )
    return (
        f"Tin starts this run without {subject}. {result.how_to_satisfy} "
        "Then start this workflow again to use it."
    )


_OUTCOMES = {
    "succeeded": "finished",
    "failed": "failed",
    "stopped": "was stopped",
    "superseded": "was replaced by a newer run",
    "needs_input": "is waiting for review",
    "paused": "is paused",
}


def _outcome(status: str | None) -> str:
    if status in _OUTCOMES:
        return _OUTCOMES[status]
    if status in ("pending", "running"):
        return f"did not finish within {PREREQUISITE_WAIT_MINUTES} minutes"
    return "is no longer available"


def _subject_key(value: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(value.get(name) for name in ("kind", "workflow", "path", "section", "host_input"))


def waited_evidence(
    evaluation: PrerequisiteEvaluation,
    *,
    workflow: Workflow,
    inputs: Mapping[str, Any],
    waited: Sequence[Mapping[str, Any]],
    statuses: Mapping[str, str | None],
    settled_at: datetime,
) -> dict[str, Any]:
    """The evidence a run pins after waiting: the fresh check, what happened, and plain notes.

    ``waited`` is the admission's ``waiting`` list; ``statuses`` maps each awaited run id to its
    status now. ``waited.refused`` is set when a required prerequisite is still unmet; the run
    then stops before it does any work.
    """
    record = evaluation.evidence(inputs=inputs)
    results = {_subject_key(_subject(r.prerequisite)): r for r in evaluation.results}
    notes, refusals = [], []
    for view in waited:
        run_id = str(view.get("running_run_id"))
        what = f"{view.get('workflow_key')} run {run_id} {_outcome(statuses.get(run_id))}"
        result = results.get(_subject_key(view))
        if result is None or not result.unmet:
            done = statuses.get(run_id) == "succeeded"
            notes.append(
                f"{what}; this run reads its result."
                if done
                else f"{what}, but another run meets the prerequisite; this run reads that one."
            )
        elif result.prerequisite.required:
            refusals.append(what)
        else:
            notes.append(f"Tin runs this without {_missing_subject(result.prerequisite)}: {what}.")
    subjects = {_subject_key(view) for view in waited}
    notes += [
        view["note"]
        for view in record["advisories"]
        if view.get("note") and _subject_key(view) not in subjects
    ]
    refused = None
    if evaluation.blocking:
        missing = PrerequisiteError.from_evaluation(evaluation, workflow=workflow, inputs=inputs)
        refused = (
            "This run stopped before doing any work"
            + (f": {'; '.join(dict.fromkeys(refusals))}. " if refusals else ". ")
            + str(missing)
        )
    record["notes"] = list(dict.fromkeys(notes))
    record["waited"] = {
        "run_ids": list(statuses),
        "statuses": dict(statuses),
        "settled_at": settled_at.isoformat(),
        "refused": refused,
    }
    return record


def _suggested_call(upstream: Workflow, *, inputs: Mapping[str, Any]) -> dict[str, Any]:
    properties = (upstream.definition.get("input_schema") or {}).get("properties") or {}
    carried = {
        name: inputs[name]
        for name in properties
        if name != "project_id" and name in inputs and inputs[name] not in (None, "")
    }
    return {"tool": "start_workflow", "workflow_id": upstream.key, "inputs": carried}
