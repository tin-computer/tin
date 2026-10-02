from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from tin_lite.activities import TinActivities
from tin_lite.catalog import BUILTIN_WORKFLOWS, VISIBILITY_AUDIT_WORKFLOW_ID
from tin_lite.domain import EffectReceipt, Project, RunStatus, WorkflowRun
from tin_lite.visibility import (
    VisibilityAuditor,
    VisibilityProtocolError,
    VisibilitySource,
    validate_visibility_report,
)
from tin_lite.workflow_inputs import normalize_workflow_inputs


def panel() -> dict:
    return {
        "target": {"name": "Virvid", "domain": "virvid.app", "aliases": []},
        "questions": [
            {
                "id": "q1",
                "family": "best_tool",
                "fit": "strong",
                "text": "What is the best tool for turning product knowledge into useful actions?",
            },
            {
                "id": "q2",
                "family": "alternatives",
                "fit": "strong",
                "text": "What are good alternatives to a manual operations playbook for founders?",
            },
            {
                "id": "q3",
                "family": "problem",
                "fit": "strong",
                "text": (
                    "How can a small team automate recurring growth work without losing context?"
                ),
            },
            {
                "id": "q4",
                "family": "provider",
                "fit": "adjacent",
                "text": "Which providers help teams run reliable AI-assisted operating routines?",
            },
            {
                "id": "q5",
                "family": "stack",
                "fit": "adjacent",
                "text": (
                    "What should be in an AI operations stack for an early-stage software company?"
                ),
            },
        ],
    }


def response_text(response_id: str, text: str, *, searched: bool = False) -> dict:
    output: list[dict] = []
    if searched:
        output.append(
            {
                "type": "web_search_call",
                "action": {
                    "query": "AI operations tools",
                    "sources": [{"url": "https://example.com/tools", "title": "Tools"}],
                },
            }
        )
    output.append(
        {
            "type": "message",
            "content": [
                {
                    "type": "output_text",
                    "text": text,
                    "annotations": (
                        [
                            {
                                "type": "url_citation",
                                "url": "https://example.com/tools",
                                "title": "Tools",
                            }
                        ]
                        if searched
                        else []
                    ),
                }
            ],
        }
    )
    return {"id": response_id, "output": output, "usage": {"total_tokens": 10}}


def adjudication() -> dict:
    return {
        "summary": "Virvid is discoverable in search but rarely reaches the recommendation set.",
        "outcomes": [
            {
                "question_id": f"q{number}",
                "found": True,
                "mentioned": number == 1,
                "evaluated": False,
                "shortlisted": False,
                "top_choice": False,
                "notes": "The source appears, but the answer does not evaluate it.",
            }
            for number in range(1, 6)
        ],
        "recommendations": [
            {
                "title": "Publish comparison evidence",
                "action": "Add a concise page that supports the strongest buyer question.",
                "evidence_question_ids": ["q1"],
            }
        ],
    }


class FakeResponses:
    model = "gpt-6-luna"

    def __init__(self, *responses: dict) -> None:
        self.responses = list(responses)
        self.payloads: list[dict] = []

    async def create(self, payload: dict) -> dict:
        self.payloads.append(payload)
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_visibility_auditor_uses_blind_forced_search_and_builds_artifacts() -> None:
    responses = FakeResponses(
        response_text("resp_panel", json.dumps(panel())),
        response_text("resp_web", "Virvid is one product to evaluate.", searched=True),
        response_text("resp_probe", "Consider established workflow automation tools."),
        response_text("resp_score", json.dumps(adjudication())),
    )
    auditor = VisibilityAuditor(responses=responses, skill_suite="# Audit rules")
    sources = [
        VisibilitySource(
            "project memory",
            "code.storage://projects/test@abc/wiki/INDEX.md",
            "# Project memory\n\nVirvid is at virvid.app.\n",
        )
    ]

    prepared = await auditor.prepare_panel(
        project_name="virvid.app",
        target_request="virvid.app",
        sources=sources,
    )
    question = prepared["questions"][0]["text"]
    searched = await auditor.answer(question=question, searched=True)
    probe = await auditor.answer(question=question, searched=False)
    measurements = [
        {"question_id": item["id"], "searched": searched, "probe": probe}
        for item in prepared["questions"]
    ]
    scored = await auditor.adjudicate(panel=prepared, measurements=measurements)
    report, evidence = auditor.build_artifacts(
        run_id="run-1",
        project_name="virvid.app",
        target_request="virvid.app",
        source_refs=[sources[0].artifact_ref],
        panel=prepared,
        measurements=measurements,
        adjudication=scored,
        evidence_path="reports/visibility/run-1/evidence.json",
    )

    panel_payload, web_payload, probe_payload, score_payload = responses.payloads
    assert panel_payload["store"] is False
    assert panel_payload["text"]["format"]["strict"] is True
    panel_reference = json.loads(panel_payload["input"][0]["content"][0]["text"])
    assert panel_reference["target_request"] == "virvid.app"
    assert "Virvid" not in web_payload["input"]
    assert web_payload["tools"] == [{"type": "web_search"}]
    assert web_payload["tool_choice"] == {"type": "web_search"}
    assert web_payload["include"] == ["web_search_call.action.sources"]
    assert "tools" not in probe_payload
    assert score_payload["text"]["format"]["name"] == "visibility_adjudication"
    assert searched["search_calls"] == 1
    assert searched["sources_returned"] == 1
    assert scored["outcomes"][0]["mentioned"] is True
    validate_visibility_report(report, evidence_path="reports/visibility/run-1/evidence.json")
    evidence_value = json.loads(evidence)
    assert evidence_value["schema_version"] == "1.1"
    assert evidence_value["target_request"] == "virvid.app"
    assert evidence_value["panel"]["panel_hash"] == prepared["panel_hash"]
    assert b"**Resolved target:** Virvid (virvid.app)" in report
    assert b"\n  " not in evidence


@pytest.mark.asyncio
async def test_visibility_panel_accepts_an_unknown_domain_without_inventing_one() -> None:
    no_domain = panel()
    no_domain["target"]["name"] = "Checkpoint A"
    no_domain["target"]["domain"] = ""
    auditor = VisibilityAuditor(
        responses=FakeResponses(response_text("resp_panel", json.dumps(no_domain))),
        skill_suite="# Audit rules",
    )

    prepared = await auditor.prepare_panel(
        project_name="Container",
        target_request="Checkpoint A",
        sources=[],
    )

    assert prepared["target"] == {"name": "Checkpoint A", "domain": "", "aliases": []}


@pytest.mark.asyncio
async def test_visibility_panel_rejects_a_different_resolved_domain() -> None:
    auditor = VisibilityAuditor(
        responses=FakeResponses(response_text("resp_panel", json.dumps(panel()))),
        skill_suite="# Audit rules",
    )

    with pytest.raises(VisibilityProtocolError, match="different target domain"):
        await auditor.prepare_panel(
            project_name="Container",
            target_request="https://linear.app/product",
            sources=[],
        )


@pytest.mark.asyncio
async def test_visibility_panel_rejects_a_different_resolved_name() -> None:
    auditor = VisibilityAuditor(
        responses=FakeResponses(response_text("resp_panel", json.dumps(panel()))),
        skill_suite="# Audit rules",
    )

    with pytest.raises(VisibilityProtocolError, match="different target"):
        await auditor.prepare_panel(
            project_name="Container",
            target_request="Linear",
            sources=[],
        )


def test_visibility_workflow_exposes_one_editable_target_with_a_project_default() -> None:
    workflow = next(item for item in BUILTIN_WORKFLOWS if item.id == VISIBILITY_AUDIT_WORKFLOW_ID)
    schema = workflow.definition["input_schema"]

    assert workflow.version_label == "1.3.0"
    # Folded into the organic audit's buyer questions: saved schedules keep running, new
    # setups and discovery no longer offer it.
    assert workflow.definition["public_discovery"] is False
    assert schema["required"] == ["project_id", "target"]
    assert schema["properties"]["target"] == {
        "type": "string",
        "minLength": 1,
        "maxLength": 200,
        "default": "this project",
        "title": "Target",
        "description": (
            "Product, company, domain, or URL to audit. Keep “this project” to derive it "
            "from durable project context."
        ),
        "x-tin-ui": {"control": "text", "order": 10},
    }
    assert normalize_workflow_inputs(schema=schema, project_id=uuid4(), inputs={}) == {
        "target": "this project"
    }


class FakeDatabase:
    def __init__(self, *, project: Project, run: WorkflowRun) -> None:
        self.project = project
        self.run = run
        self.receipts: dict[str, EffectReceipt] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.events: list[str] = []
        self.projection_writes = 0

    @asynccontextmanager
    async def effect_lock(
        self, execution_key: str, operation: str
    ) -> AsyncIterator[tuple[None, EffectReceipt | None]]:
        async with self._locks.setdefault(execution_key, asyncio.Lock()):
            yield None, self.receipts.get(execution_key)

    async def start_effect(self, conn, *, execution_key: str, operation: str) -> None:
        self.receipts.setdefault(
            execution_key,
            EffectReceipt(execution_key, operation, "started", None),
        )

    async def complete_effect(self, conn, *, execution_key: str, result: dict) -> None:
        operation = self.receipts[execution_key].operation
        self.receipts[execution_key] = EffectReceipt(execution_key, operation, "completed", result)

    async def fail_effect(self, conn, *, execution_key: str, error_message: str) -> None:
        operation = self.receipts[execution_key].operation
        self.receipts[execution_key] = EffectReceipt(execution_key, operation, "failed", None)

    @asynccontextmanager
    async def project_state_lock(self, conn, project_id: UUID) -> AsyncIterator[None]:
        assert project_id == self.project.id
        yield

    async def get_run(self, run_id: UUID):
        return self.run if run_id == self.run.id else None

    async def get_project(self, project_id: UUID):
        return self.project if project_id == self.project.id else None

    async def mark_run_running(self, run_id: UUID) -> None:
        self.run = replace(self.run, status=RunStatus.RUNNING)

    async def list_memory_source_runs(self, **values):
        raise AssertionError("ready project memory should be the only visibility source")

    async def get_effect(self, execution_key: str, *, conn=None):
        return self.receipts.get(execution_key)

    async def project_success(self, **values) -> None:
        self.projection_writes += 1
        self.run = replace(
            self.run,
            status=RunStatus.SUCCEEDED,
            canonical_commit_sha=values["canonical_commit_sha"],
            artifact_ref=values["artifact_ref"],
            artifact_path=values["artifact_path"],
        )

    async def complete_visibility_projection(self, conn, *, execution_key, **values) -> None:
        await self.project_success(**values)
        await self.add_activity(event_type="visibility_audit_ready")
        await self.complete_effect(
            conn, execution_key=execution_key, result={"artifact_ref": values["artifact_ref"]}
        )

    async def add_activity(self, *, event_type: str, **values) -> None:
        self.events.append(event_type)


class FakeStorage:
    def __init__(self) -> None:
        self.documents: dict[str, bytes] = {}
        self.publishes = 0

    async def read_canonical_artifact(self, *, path: str, **values) -> bytes:
        if path == "workflows/visibility.audit.json":
            return b'{"key":"visibility.audit"}'
        if path == "wiki/INDEX.md":
            return b"# Project memory\n\nVirvid is at virvid.app.\n"
        return self.documents[path]

    async def publish_state_documents(self, *, documents: dict[str, bytes], **values):
        self.publishes += 1
        self.documents.update(documents)
        await asyncio.sleep(0)
        return "a" * 40, True


class FakeAuditor:
    model = "gpt-6-luna"

    def __init__(self) -> None:
        self.panel_calls = 0
        self.panel_targets: list[str] = []
        self.answer_calls = 0
        self.adjudication_calls = 0

    async def prepare_panel(self, **values) -> dict:
        self.panel_calls += 1
        self.panel_targets.append(values["target_request"])
        result = panel()
        result.update({"response_id": "resp_panel", "model": self.model, "panel_hash": "p" * 64})
        await asyncio.sleep(0)
        return result

    async def answer(self, *, searched: bool, **values) -> dict:
        self.answer_calls += 1
        await asyncio.sleep(0)
        return {
            "mode": "web" if searched else "model_only",
            "response_id": f"resp_{self.answer_calls}",
            "answer": "Virvid is worth evaluating." if searched else "Use a workflow tool.",
            "search_calls": 1 if searched else 0,
            "queries": [],
            "sources": [],
            "citations": [],
            "usage": {},
        }

    async def adjudicate(self, **values) -> dict:
        self.adjudication_calls += 1
        await asyncio.sleep(0)
        result = adjudication()
        result["response_id"] = "resp_score"
        return result

    def build_artifacts(self, *, evidence_path: str, **values) -> tuple[bytes, bytes]:
        report = (
            "# AI visibility audit\n\n"
            "## Summary\n\nSummary.\n\n"
            "## Buyer questions\n\nQuestions.\n\n"
            "## Primary bottleneck\n\nDiscovery.\n\n"
            "## Recommended next move\n\nPublish evidence.\n\n"
            f"## Evidence\n\nRaw run evidence: `{evidence_path}`\n\n"
            "## Method\n\nFive target-blind questions.\n"
        ).encode()
        return report, json.dumps(
            {
                "schema_version": "1.1",
                "workflow": "visibility.audit",
                "run_id": values["run_id"],
                "panel": values["panel"],
                "measurements": values["measurements"],
                "adjudication": values["adjudication"],
                "source_refs": values["source_refs"],
            }
        ).encode()


def activity_fixture() -> tuple[Project, WorkflowRun]:
    project = Project(
        uuid4(),
        "virvid.app",
        "projects/test",
        "main",
        memory_commit_sha="m" * 40,
        memory_index_path="wiki/INDEX.md",
        memory_index="# Project memory\n",
    )
    run_id = uuid4()
    run = WorkflowRun(
        id=run_id,
        project_id=project.id,
        workflow_id=VISIBILITY_AUDIT_WORKFLOW_ID,
        executor="visibility.audit",
        definition_commit_sha="d" * 40,
        temporal_workflow_id=f"visibility.audit:{run_id}",
        thread_id=str(VISIBILITY_AUDIT_WORKFLOW_ID),
        generation=1,
        fencing_token=1,
        status=RunStatus.PENDING,
        input={"target": "virvid.app"},
    )
    return project, run


@pytest.mark.asyncio
async def test_visibility_duplicate_execution_reuses_calls_commit_and_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, run = activity_fixture()
    database = FakeDatabase(project=project, run=run)
    storage = FakeStorage()
    auditor = FakeAuditor()
    monkeypatch.setattr("tin_lite.activities.activity.heartbeat", lambda details: None)
    activities = TinActivities(
        database=database,  # type: ignore[arg-type]
        storage=storage,  # type: ignore[arg-type]
        sandboxes=SimpleNamespace(),
        settings=SimpleNamespace(),
        visibility_auditor=auditor,  # type: ignore[arg-type]
    )

    await asyncio.gather(
        activities.generate_visibility_audit(str(run.id)),
        activities.generate_visibility_audit(str(run.id)),
    )
    await asyncio.gather(
        activities.project_visibility_result(str(run.id)),
        activities.project_visibility_result(str(run.id)),
    )

    assert auditor.panel_calls == 1
    assert auditor.panel_targets == ["virvid.app"]
    assert auditor.answer_calls == 10
    assert auditor.adjudication_calls == 1
    assert storage.publishes == 1
    assert database.projection_writes == 1
    assert database.run.status == RunStatus.SUCCEEDED
    assert database.run.artifact_path == "reports/AI_VISIBILITY.md"
    assert database.events == ["visibility_audit_created", "visibility_audit_ready"]


@pytest.mark.asyncio
async def test_visibility_question_ids_are_normalized_and_adjudication_lines_up() -> None:
    raw = panel()
    long_id = "Buyer Question About Automation Tools"
    for item, question_id in zip(raw["questions"], ["Q1", "Q 2", "q3", "Q3", long_id], strict=True):
        item["id"] = question_id
    normalized = ["q1", "q_2", "q3", "q3-2", "buyer_question_about_automation_"]
    scored_raw = adjudication()
    for outcome, question_id in zip(scored_raw["outcomes"], normalized, strict=True):
        outcome["question_id"] = question_id
    scored_raw["outcomes"][0]["question_id"] = "Q1"  # the pre-normalization spelling
    scored_raw["recommendations"][0]["evidence_question_ids"] = ["Q1", "Q 2", "q3-2"]
    responses = FakeResponses(
        response_text("resp_panel", json.dumps(raw)),
        response_text("resp_score", json.dumps(scored_raw)),
    )
    auditor = VisibilityAuditor(responses=responses, skill_suite="# Audit rules")

    prepared = await auditor.prepare_panel(
        project_name="virvid.app", target_request="virvid.app", sources=[]
    )
    panel_schema = responses.payloads[0]["text"]["format"]["schema"]
    id_schema = panel_schema["properties"]["questions"]["items"]["properties"]["id"]
    assert id_schema["pattern"] == "^[a-z0-9_-]{1,32}$"
    assert [item["id"] for item in prepared["questions"]] == normalized
    again = await VisibilityAuditor(
        responses=FakeResponses(response_text("resp_panel", json.dumps(raw))),
        skill_suite="# Audit rules",
    ).prepare_panel(project_name="virvid.app", target_request="virvid.app", sources=[])
    assert again["panel_hash"] == prepared["panel_hash"]  # receipts keyed by these IDs

    answer = response_text("resp_web", "Virvid is one product to evaluate.", searched=True)
    searched = await VisibilityAuditor(
        responses=FakeResponses(answer), skill_suite="# Audit rules"
    ).answer(question=prepared["questions"][0]["text"], searched=True)
    measurements = [
        {"question_id": item["id"], "searched": searched, "probe": searched}
        for item in prepared["questions"]
    ]
    scored = await auditor.adjudicate(panel=prepared, measurements=measurements)
    assert [item["question_id"] for item in scored["outcomes"]] == normalized
    assert scored["recommendations"][0]["evidence_question_ids"] == ["q1", "q_2", "q3-2"]


@pytest.mark.asyncio
async def test_visibility_question_id_that_cannot_be_normalized_still_fails() -> None:
    raw = panel()
    raw["questions"][0]["id"] = 7
    auditor = VisibilityAuditor(
        responses=FakeResponses(response_text("resp_panel", json.dumps(raw))),
        skill_suite="# Audit rules",
    )
    with pytest.raises(VisibilityProtocolError, match="question ID is invalid"):
        await auditor.prepare_panel(
            project_name="virvid.app", target_request="virvid.app", sources=[]
        )
