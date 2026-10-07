from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from tin_lite.activity_lanes import ActivityLaneInterceptor
from tin_lite.codex_execution import ProjectCodexExecution
from tin_lite.workflows import (
    AnswerPageWorkflow,
    CodeWorkflow,
    CodexProcedureWorkflow,
    ConnectionCollectionWorkflow,
    DesignMdWorkflow,
    KeywordPlanWorkflow,
    OrganicAuditWorkflow,
    ProjectMemoryWorkflow,
    ProjectTaskWorkflow,
    ScanReportWorkflow,
    ScheduledDispatchWorkflow,
    VisibilityAuditWorkflow,
)

FIXTURES = Path(__file__).parent / "fixtures"
ACCEPTED_HISTORIES = (
    (
        ConnectionCollectionWorkflow,
        "connections.collect:00000000-0000-4000-8000-000000000001",
        FIXTURES / "connection_collection_v1_history.json",
    ),
    (
        CodeWorkflow,
        "workflow.code:8d24bcc6-9000-4f5d-a31b-f839f133b459",
        FIXTURES / "code_schedule_code_history.json",
    ),
    (
        ScheduledDispatchWorkflow,
        "tin-scheduled-dispatch:091763d8-1b04-44ba-a661-e9119b494b75-2026-09-16T16:16:00Z",
        FIXTURES / "code_schedule_dispatch_history.json",
    ),
    (
        CodeWorkflow,
        "workflow.code:e3532876-8401-412b-86cd-b27a28e3d0fb",
        FIXTURES / "code_connection_recovery_history.json",
    ),
    (
        ProjectCodexExecution,
        "tin.project-codex:00be2c21-f011-4d22-92b2-a8613b423edd",
        FIXTURES / "project_code_connection_recovery_history.json",
    ),
    (
        CodeWorkflow,
        "workflow.code:c9959a32-6c43-4ac8-a226-90e0f77ecad5",
        FIXTURES / "code_model_recovery_history.json",
    ),
    (
        ProjectCodexExecution,
        "tin.project-codex:00be2c21-f011-4d22-92b2-a8613b423edd",
        FIXTURES / "project_code_model_recovery_history.json",
    ),
    (
        CodeWorkflow,
        "workflow.code:4ae6be52-b11b-481c-aa97-5249c54a8550",
        FIXTURES / "code_workflow_history.json",
    ),
    (
        ProjectCodexExecution,
        "tin.project-codex:00be2c21-f011-4d22-92b2-a8613b423edd",
        FIXTURES / "project_code_execution_history.json",
    ),
    (
        DesignMdWorkflow,
        "content.design_md:25a447ce-5572-4138-8f48-794c88501a43",
        FIXTURES / "content_design_history.json",
    ),
    (
        DesignMdWorkflow,
        "content.design_md:abd13263-8a59-45d7-8c03-b3bd0b26d27a",
        FIXTURES / "content_design_recovery_history.json",
    ),
    (
        ProjectMemoryWorkflow,
        "project.memory:24c7b814-5049-49ad-9f59-666b074b35f7",
        FIXTURES / "project_memory_history.json",
    ),
    (
        ScanReportWorkflow,
        "scan.report:b30a8224-0347-4923-be10-db5483b78c75",
        FIXTURES / "scan_report_history.json",
    ),
    (
        VisibilityAuditWorkflow,
        "visibility.audit:ccd7d6a4-5706-45b2-b902-e3c9c1b161e6",
        FIXTURES / "visibility_audit_history.json",
    ),
    (
        AnswerPageWorkflow,
        "content.answer_page:87ff41d8-5109-4d5e-a147-ab941b4849e2",
        FIXTURES / "answer_page_history.json",
    ),
    (
        AnswerPageWorkflow,
        "content.answer_page:22f09a94-8540-4f34-b14e-605668403ee2",
        FIXTURES / "answer_page_review_history.json",
    ),
    (
        ProjectTaskWorkflow,
        "project.task:4d1a1055-bdb3-4261-ab67-901cc777194b",
        FIXTURES / "project_task_history.json",
    ),
    (
        CodexProcedureWorkflow,
        "codex.procedure:bd7a758e-671c-4638-82c1-f4c5a227bcf9",
        FIXTURES / "codex_procedure_history.json",
    ),
    (
        CodexProcedureWorkflow,
        "codex.procedure:418719c1-4dbf-44fc-9833-b012b2a0f299",
        FIXTURES / "codex_procedure_review_history.json",
    ),
    # Recorded locally before keyword-inspect-batch-v1: one keyword_inspect per sample.
    (
        KeywordPlanWorkflow,
        "organic.keyword_plan:5f0b2c8e-3d4a-4f6b-9a1e-7c2d8e9f0a1b",
        FIXTURES / "keyword_plan_history.json",
    ),
    # Recorded locally from main at d69d337, before organic-audit-ai-engines-v1: an audit
    # that published without asking any AI engine.
    (
        OrganicAuditWorkflow,
        "organic.audit:6b1f0c2e-8d3a-4e5f-9a7b-2c4d6e8f0a13",
        FIXTURES / "organic_audit_history.json",
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(("workflow_type", "workflow_id", "history_path"), ACCEPTED_HISTORIES)
async def test_current_workflow_replays_accepted_cloud_history(
    workflow_type: type, workflow_id: str, history_path: Path
) -> None:
    history_json = json.loads(await anyio.Path(history_path).read_text())

    await Replayer(
        workflows=[workflow_type], interceptors=[ActivityLaneInterceptor()]
    ).replay_workflow(WorkflowHistory.from_json(workflow_id, history_json))
