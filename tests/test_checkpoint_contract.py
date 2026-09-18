from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from temporalio.exceptions import WorkflowAlreadyStartedError

from tin_lite.activities import TinActivities
from tin_lite.api import router
from tin_lite.auth import AuthContext, require_user
from tin_lite.catalog import (
    ANSWER_PAGE_WORKFLOW_ID,
    BUILTIN_WORKFLOWS,
    COLD_OUTREACH_SYSTEM,
    DESIGN_MD_WORKFLOW_ID,
    ORGANIC_TRAFFIC_SYSTEM,
    SCAN_REPORT_WORKFLOW_ID,
    START_HERE_SYSTEM,
    VISIBILITY_AUDIT_WORKFLOW_ID,
    WORKFLOW_SYSTEMS,
    sync_builtin_workflows,
)
from tin_lite.codex_execution import ProjectCodexExecution
from tin_lite.db import _effect_receipt
from tin_lite.domain import (
    CODEX_PROCEDURE_EXECUTOR,
    EffectReceipt,
    Project,
    RunStatus,
    StaleGenerationError,
    Workflow,
    WorkflowRun,
    WorkflowStatus,
)
from tin_lite.e2b_runtime import _run_secrets
from tin_lite.integrations import IntegrationAuthorizationError
from tin_lite.procedures import PinnedCodexProcedure
from tin_lite.rollouts import RolloutCapture, RolloutFile
from tin_lite.system_wiki import SystemWikiRef
from tin_lite.workflows import (
    AnswerPageWorkflow,
    CharacterDesignWorkflow,
    CodeWorkflow,
    CodexProcedureWorkflow,
    ContentDraftDeliveryWorkflow,
    ContentPlanWorkflow,
    DesignMdWorkflow,
    EmailCampaignWorkflow,
    EmailRecipientWorkflow,
    GrowthOnboardingPlanWorkflow,
    GrowthOnboardingWorkflow,
    KeywordPlanWorkflow,
    OrganicAuditWorkflow,
    OrganicTrafficSystemWorkflow,
    ProjectMemoryWorkflow,
    ProjectTaskWorkflow,
    ScanReportWorkflow,
    ScheduledDispatchWorkflow,
    SiteHealthWorkflow,
    StyleCaptureWorkflow,
    VisibilityAuditWorkflow,
    WeeklyBriefWorkflow,
    registered_workflow_implementations,
    registered_workflows,
)


class FakeDatabase:
    def __init__(self, *, run: WorkflowRun, project: Project, workflow: Workflow) -> None:
        self.run = run
        self.project = project
        self.workflow = workflow
        self.receipts: dict[str, EffectReceipt] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.projection_writes = 0
        self.events: list[str] = []
        self.event_details: dict[str, dict] = {}
        self.rollout_inserts: list[dict] = []

    @asynccontextmanager
    async def effect_lock(
        self, execution_key: str, operation: str
    ) -> AsyncIterator[tuple[None, EffectReceipt | None]]:
        lock = self._locks.setdefault(execution_key, asyncio.Lock())
        async with lock:
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

    async def get_run(self, run_id):
        return self.run if run_id == self.run.id else None

    async def get_project(self, project_id):
        return self.project if project_id == self.project.id else None

    async def has_project_access(self, *, project_id, clerk_user_id):
        return project_id == self.project.id and clerk_user_id == "user_test"

    async def get_workflow(self, workflow_id):
        return self.workflow if workflow_id == self.workflow.id else None

    async def get_registry_workflow(self, key):
        return self.workflow if key == self.workflow.key else None

    async def list_workflows(self, *, project_id=None):
        return [self.workflow]

    async def get_run_by_start_key(self, **values):
        return None

    async def create_run(
        self,
        *,
        project_id,
        workflow_id,
        started_by_clerk_user_id=None,
        start_idempotency_key=None,
        definition_commit_sha=None,
        pinned_definition=None,
    ):
        if project_id != self.project.id or workflow_id != self.workflow.id:
            raise LookupError("workflow is not available")
        assert started_by_clerk_user_id == "user_test"
        assert start_idempotency_key is None
        assert definition_commit_sha == self.workflow.current_commit_sha
        assert pinned_definition == self.workflow.definition
        return self.run, True

    async def project_failure(self, *, run_id, error_message):
        self.run = replace(self.run, status=RunStatus.FAILED, error_message=error_message)

    async def validate_lease(self, **values) -> bool:
        return self.run.lease_active and all(
            getattr(self.run, name) == value for name, value in values.items()
        )

    async def get_effect(self, execution_key: str):
        return self.receipts.get(execution_key)

    async def attach_sandbox(
        self,
        *,
        run_id,
        sandbox_id,
        lease_owner,
        expected_head_sha,
        ephemeral_branch,
    ):
        assert run_id == self.run.id
        self.run = replace(
            self.run,
            sandbox_id=sandbox_id,
            lease_owner=lease_owner,
            expected_head_sha=expected_head_sha,
            ephemeral_branch=ephemeral_branch,
        )
        return self.run

    async def project_success(self, **values) -> None:
        self.projection_writes += 1
        self.run = replace(
            self.run,
            status=RunStatus.SUCCEEDED,
            canonical_commit_sha=values["canonical_commit_sha"],
            artifact_ref=values["artifact_ref"],
            artifact_path=values["artifact_path"],
        )

    async def add_activity(self, *, run_id, event_type: str, **values) -> None:
        self.events.append(event_type)
        self.event_details[event_type] = values

    async def insert_run_rollouts(self, **values) -> int:
        self.rollout_inserts.append(values)
        return len(values["files"])

    async def release_lease(self, run_id) -> None:
        self.run = replace(self.run, lease_active=False)


class FakeStorage:
    def __init__(self) -> None:
        self.canonical_commits = 0
        self.recoverable_artifact: bytes | None = None

    async def read_ephemeral_artifact_if_exists(self, **values) -> bytes | None:
        return self.recoverable_artifact

    async def read_ephemeral_artifact(self, **values) -> bytes:
        return b"# Product design\n"

    def sandbox_remotes(self, **values):
        return SimpleNamespace(
            canonical_url="https://storage.test/canonical",
            canonical_auth_header="Authorization: Basic canonical",
            ephemeral_url="https://storage.test/ephemeral",
            ephemeral_auth_header="Authorization: Basic ephemeral",
        )

    async def create_canonical_commit(self, **values) -> str:
        self.canonical_commits += 1
        await asyncio.sleep(0)
        return "c" * 40


class FakeSandboxes:
    def __init__(self, *, next_sandbox_id: str = "sandbox-2") -> None:
        self.killed: list[str] = []
        self.next_sandbox_id = next_sandbox_id
        self.ran: list[str] = []

    async def create(self, **values) -> str:
        return self.next_sandbox_id

    async def run_and_kill(self, *, sandbox_id: str, run_input) -> str:
        self.ran.append(sandbox_id)
        # The real runtime derives the redaction set from the run input and hands the
        # captured rollouts to the sink before killing the sandbox.
        secrets = _run_secrets(run_input)
        assert "Authorization: Basic ephemeral" in secrets
        assert "http://proxy.test:8888" in secrets
        if run_input.rollout_sink is not None:
            await run_input.rollout_sink(
                RolloutCapture(
                    sandbox_id=sandbox_id,
                    files=(
                        RolloutFile(
                            path="/home/user/.codex/sessions/2026/09/04/rollout-x.jsonl",
                            filename="rollout-x.jsonl",
                            thread_id=None,
                            content=b"{}\n",
                            size_bytes=3,
                        ),
                    ),
                ),
            )
        self.killed.append(sandbox_id)
        return "e" * 40

    async def is_running(self, sandbox_id: str) -> bool:
        return False

    async def kill(self, sandbox_id: str) -> None:
        self.killed.append(sandbox_id)


def fixture_state(active: bool = True):
    project_id = uuid4()
    run_id = uuid4()
    project = Project(project_id, "Test", "projects/test", "main")
    workflow = Workflow(
        id=DESIGN_MD_WORKFLOW_ID,
        project_id=None,
        key="content.design_md",
        title="Generate project design",
        description="Test workflow",
        executor="content.design_md",
        definition_repo_id="registry/workflows",
        definition_path="workflows/content.design_md.json",
        current_commit_sha="d" * 40,
        version_label="1.0.0",
        definition=next(
            item.definition for item in BUILTIN_WORKFLOWS if item.id == DESIGN_MD_WORKFLOW_ID
        ),
        status=WorkflowStatus.ACTIVE,
    )
    run = WorkflowRun(
        id=run_id,
        project_id=project_id,
        workflow_id=workflow.id,
        executor="content.design_md",
        definition_commit_sha="d" * 40,
        temporal_workflow_id=f"content.design_md:{run_id}",
        thread_id=str(workflow.id),
        generation=2,
        fencing_token=22,
        status=RunStatus.RUNNING,
        sandbox_id="sandbox-2",
        lease_owner=f"{run_id}:sandbox_create",
        lease_active=active,
        ephemeral_branch=f"generations/{run_id}/2",
        expected_head_sha="a" * 40,
    )
    return project, workflow, run


def authenticate(app: FastAPI) -> None:
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id="user_test",
        token_type="session_token",  # noqa: S106
        session_id="sess_test",
        raw_token="test-session-token",  # noqa: S106
    )


@pytest.mark.asyncio
async def test_forced_duplicate_creates_one_canonical_commit_and_projection() -> None:
    project, workflow, run = fixture_state()
    database = FakeDatabase(run=run, project=project, workflow=workflow)
    storage = FakeStorage()
    activities = TinActivities(
        database=database,
        storage=storage,
        sandboxes=SimpleNamespace(),
        settings=SimpleNamespace(),
    )

    await asyncio.gather(
        activities.commit_design_canonically(str(run.id)),
        activities.commit_design_canonically(str(run.id)),
    )
    await asyncio.gather(
        activities.project_design_result(str(run.id)),
        activities.project_design_result(str(run.id)),
    )

    assert storage.canonical_commits == 1
    assert database.projection_writes == 1
    assert database.run.status == RunStatus.SUCCEEDED
    assert database.run.canonical_commit_sha == "c" * 40


@pytest.mark.asyncio
async def test_stale_generation_is_rejected_before_canonical_write() -> None:
    project, workflow, run = fixture_state(active=False)
    database = FakeDatabase(run=run, project=project, workflow=workflow)
    storage = FakeStorage()
    activities = TinActivities(
        database=database,
        storage=storage,
        sandboxes=SimpleNamespace(),
        settings=SimpleNamespace(),
    )

    with pytest.raises(StaleGenerationError):
        await activities.commit_design_canonically(str(run.id))

    assert storage.canonical_commits == 0
    assert database.projection_writes == 0


@pytest.mark.asyncio
async def test_stale_codex_procedure_is_rejected_before_canonical_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, workflow, run = fixture_state(active=False)
    workflow = replace(
        workflow,
        key="research.deep_dive",
        executor=CODEX_PROCEDURE_EXECUTOR,
        definition_path="workflows/research.deep_dive.json",
    )
    run = replace(run, workflow_id=workflow.id, executor=CODEX_PROCEDURE_EXECUTOR)
    database = FakeDatabase(run=run, project=project, workflow=workflow)
    database.receipts[f"{run.id}:procedure_artifact_persist"] = EffectReceipt(
        f"{run.id}:procedure_artifact_persist",
        "procedure_artifact_persist",
        "completed",
        {"summary": "Research ready."},
    )
    storage = FakeStorage()
    activities = TinActivities(
        database=database,
        storage=storage,
        sandboxes=SimpleNamespace(),
        settings=SimpleNamespace(),
    )
    procedure = PinnedCodexProcedure(
        workflow_key=workflow.key,
        prompt="Research.",
        entry_skill="research-deep-dive",
        skill_files={"research-deep-dive/SKILL.md": b"name: research-deep-dive\n"},
        output_path="reports/RESEARCH.md",
        output_media_type="text/markdown",
        output_max_bytes=100_000,
        project_skills=(),
    )

    async def pinned(run_id):
        assert run_id == run.id
        return workflow, procedure

    monkeypatch.setattr(activities, "_pinned_codex_procedure", pinned)

    with pytest.raises(StaleGenerationError):
        await activities.commit_codex_procedure_artifact(str(run.id))

    assert storage.canonical_commits == 0


@pytest.mark.asyncio
async def test_failure_projection_cleans_orphan_and_releases_lease() -> None:
    project, workflow, run = fixture_state()
    database = FakeDatabase(run=run, project=project, workflow=workflow)
    sandboxes = FakeSandboxes()
    activities = TinActivities(
        database=database,
        storage=FakeStorage(),
        sandboxes=sandboxes,
        settings=SimpleNamespace(),
    )

    await activities.project_design_failure(
        {"run_id": str(run.id), "reason": "ActivityError: workflow failed"}
    )

    assert sandboxes.killed == ["sandbox-2"]
    assert database.run.status == RunStatus.FAILED
    assert database.run.lease_active is False


@pytest.mark.asyncio
async def test_persist_reconciles_durable_ephemeral_branch_without_rerunning_codex() -> None:
    project, workflow, run = fixture_state()
    database = FakeDatabase(run=run, project=project, workflow=workflow)
    storage = FakeStorage()
    storage.recoverable_artifact = b"# Recovered design\n"
    sandboxes = FakeSandboxes()
    activities = TinActivities(
        database=database,
        storage=storage,
        sandboxes=sandboxes,
        settings=SimpleNamespace(),
    )

    await activities.persist_design_artifact(str(run.id))

    receipt = database.receipts[f"{run.id}:artifact_persist"]
    assert receipt.status == "completed"
    assert receipt.result == {
        "ephemeral_branch": run.ephemeral_branch,
        "reconciled_from_ephemeral_branch": True,
    }
    assert sandboxes.ran == []
    assert sandboxes.killed == ["sandbox-2"]
    assert database.events == ["artifact_persisted", "sandbox_killed"]


@pytest.mark.asyncio
async def test_legacy_oauth_persist_refuses_replacement_before_compute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, workflow, run = fixture_state()
    database = FakeDatabase(run=run, project=project, workflow=workflow)
    storage = FakeStorage()
    sandboxes = FakeSandboxes(next_sandbox_id="sandbox-3")
    heartbeats: list[dict] = []
    monkeypatch.setattr("tin_lite.activities.activity.heartbeat", heartbeats.append)
    settings = SimpleNamespace(
        switchboard_public_url="https://tin.test",
        forward_proxy_url=SimpleNamespace(get_secret_value=lambda: "http://proxy.test:8888"),
    )
    activities = TinActivities(
        database=database,
        storage=storage,
        sandboxes=sandboxes,
        settings=settings,
    )

    with pytest.raises(ValueError, match="retired"):
        await activities.persist_design_artifact(str(run.id))

    assert database.run.sandbox_id == run.sandbox_id
    assert not sandboxes.ran
    assert not heartbeats
    assert not database.rollout_inserts
    assert database.receipts[f"{run.id}:artifact_persist"].status == "started"


def test_workflow_registry_is_explicit_and_narrow() -> None:
    assert registered_workflows() == [
        CodeWorkflow,
        ContentDraftDeliveryWorkflow,
        ProjectCodexExecution,
        StyleCaptureWorkflow,
        OrganicTrafficSystemWorkflow,
        GrowthOnboardingWorkflow,
        GrowthOnboardingPlanWorkflow,
        ScheduledDispatchWorkflow,
        ContentPlanWorkflow,
        DesignMdWorkflow,
        ProjectMemoryWorkflow,
        ScanReportWorkflow,
        SiteHealthWorkflow,
        VisibilityAuditWorkflow,
        OrganicAuditWorkflow,
        KeywordPlanWorkflow,
        AnswerPageWorkflow,
        CharacterDesignWorkflow,
        CodexProcedureWorkflow,
        WeeklyBriefWorkflow,
        ProjectTaskWorkflow,
        EmailCampaignWorkflow,
        EmailRecipientWorkflow,
    ]
    assert registered_workflow_implementations() == {
        "style.capture": StyleCaptureWorkflow,
        "organic.traffic_system": OrganicTrafficSystemWorkflow,
        "growth.onboarding": GrowthOnboardingWorkflow,
        "growth.onboarding_plan": GrowthOnboardingPlanWorkflow,
        "content.plan": ContentPlanWorkflow,
        "content.design_md": DesignMdWorkflow,
        "project.memory": ProjectMemoryWorkflow,
        "scan.report": ScanReportWorkflow,
        "site.health_improve": SiteHealthWorkflow,
        "visibility.audit": VisibilityAuditWorkflow,
        "organic.audit": OrganicAuditWorkflow,
        "organic.keyword_plan": KeywordPlanWorkflow,
        "content.answer_page": AnswerPageWorkflow,
        "creative.character": CharacterDesignWorkflow,
        CODEX_PROCEDURE_EXECUTOR: CodexProcedureWorkflow,
        "workflow.code": CodeWorkflow,
        "project.weekly_brief": WeeklyBriefWorkflow,
        "project.task": ProjectTaskWorkflow,
        "outreach.email_campaign": EmailCampaignWorkflow,
    }
    assert {item.executor for item in BUILTIN_WORKFLOWS}.issubset(
        registered_workflow_implementations()
    )
    assert CODEX_PROCEDURE_EXECUTOR in registered_workflow_implementations()


@pytest.mark.asyncio
async def test_builtin_sync_keeps_the_immutable_definition_commit(monkeypatch) -> None:
    # This fixture models the native catalog, not a deployment's selected public packages.
    monkeypatch.setattr("tin_lite.public_workflows.PUBLIC_WORKFLOWS", ())
    _, workflow, _ = fixture_state()

    class FakeCatalogDatabase:
        def __init__(self):
            self.synced = []
            self.synced_systems = []

        async def upsert_workflow_system(self, **values):
            self.synced_systems.append(values)

        async def get_workflow(self, workflow_id):
            builtin = next(item for item in BUILTIN_WORKFLOWS if item.id == workflow_id)
            return replace(
                workflow,
                id=workflow_id,
                key=builtin.key,
                executor=builtin.executor,
                definition_path=builtin.definition_path,
            )

        async def upsert_registry_workflow(self, **values):
            self.synced.append(values)

    class FakeCatalogStorage:
        def __init__(self):
            self.known_commit_sha = None

        async def publish_workflow_files(self, **values):
            self.known_commit_sha = values["known_commit_sha"]
            return values["known_commit_sha"]

    database = FakeCatalogDatabase()
    storage = FakeCatalogStorage()
    await sync_builtin_workflows(
        database=database,
        storage=storage,
        system_wiki=SystemWikiRef("wiki/system", "growth/project-scanning.md", "w" * 40),
    )

    assert storage.known_commit_sha == "d" * 40
    assert database.synced_systems == [
        {
            "system_id": system.id,
            "name": system.name,
            "display_order": system.display_order,
        }
        for system in WORKFLOW_SYSTEMS
    ]
    scan = next(item for item in database.synced if item["workflow_id"] == SCAN_REPORT_WORKFLOW_ID)
    visibility = next(
        item for item in database.synced if item["workflow_id"] == VISIBILITY_AUDIT_WORKFLOW_ID
    )
    answer_page = next(
        item for item in database.synced if item["workflow_id"] == ANSWER_PAGE_WORKFLOW_ID
    )
    assert scan["current_commit_sha"] == "d" * 40
    assert scan["definition"]["system_wiki"]["commit_sha"] == "w" * 40
    assert "system_wiki" not in visibility["definition"]
    assert "human_review" not in visibility["definition"]
    assert answer_page["definition"]["input_schema"]["required"] == ["project_id"]
    assert answer_page["definition"]["human_review"] == {
        "eligible": True,
        "reason": "Produces customer-facing content.",
        "review_label": "Review draft",
        "defer_label": "Not now",
        "summary": (
            "Your answer-page draft is ready. Review it before Tin marks the workflow "
            "complete; otherwise it stays safely on hold."
        ),
        "queue_clause": "Answer-page draft ready to finish",
    }
    assert "not for general advice" in answer_page["description"]


def test_registry_system_assignments_are_manifest_metadata_only() -> None:
    definitions = {item.key: item.definition for item in BUILTIN_WORKFLOWS}

    assert definitions["growth.onboarding"]["system"] == START_HERE_SYSTEM
    assert definitions["growth.onboarding_plan"]["system"] == START_HERE_SYSTEM
    # Start-here workflows are agent-only: the MCP lists them, the product catalog does not.
    assert definitions["growth.onboarding"]["agent_only"] is True
    assert definitions["growth.onboarding_plan"]["agent_only"] is True
    assert "agent_only" not in definitions["visibility.audit"]
    assert definitions["site.health_improve"]["system"] == ORGANIC_TRAFFIC_SYSTEM
    assert definitions["visibility.audit"]["system"] == ORGANIC_TRAFFIC_SYSTEM
    assert definitions["content.answer_page"]["system"] == ORGANIC_TRAFFIC_SYSTEM
    assert definitions["outreach.email_shortlist"]["system"] == COLD_OUTREACH_SYSTEM
    assert definitions["outreach.email_campaign"]["system"] == COLD_OUTREACH_SYSTEM
    assert "system" not in definitions["research.deep_dive"]
    assert definitions["content.public_article"]["system"] == ORGANIC_TRAFFIC_SYSTEM
    assert definitions["style.capture"]["system"] == ORGANIC_TRAFFIC_SYSTEM

    migration = (Path(__file__).parents[1] / "migrations" / "017_workflow_systems.sql").read_text()
    assert "CREATE TABLE workflow_systems" in migration
    assert "ALTER TABLE workflows" not in migration
    assert "system_id" not in migration


def test_effect_receipt_decodes_asyncpg_jsonb_text() -> None:
    receipt = _effect_receipt(
        {
            "execution_key": "run:canonical_commit",
            "operation": "canonical_commit",
            "status": "completed",
            "result": '{"canonical_commit_sha":"abc123"}',
        }
    )

    assert receipt.result == {"canonical_commit_sha": "abc123"}


@pytest.mark.asyncio
async def test_status_endpoint_reads_postgres_without_touching_temporal() -> None:
    project, workflow, run = fixture_state()
    database = FakeDatabase(run=run, project=project, workflow=workflow)

    class TemporalMustNotBeRead:
        def __getattr__(self, name):
            raise AssertionError(f"status endpoint touched Temporal: {name}")

    app = FastAPI()
    app.include_router(router)
    authenticate(app)
    app.state.runtime = SimpleNamespace(
        database=database,
        temporal=TemporalMustNotBeRead(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/workflows/runs/{run.id}")
        card = await client.get(f"/runs/{run.id}")

    assert response.status_code == 200
    assert response.headers["X-Tin-Read-Source"] == "postgres"
    assert response.json()["id"] == str(run.id)
    assert card.status_code == 200
    assert card.headers["X-Tin-Read-Source"] == "postgres"
    assert str(run.id) in card.text


@pytest.mark.asyncio
async def test_workflow_catalog_reads_postgres_without_touching_temporal() -> None:
    project, workflow, run = fixture_state()
    database = FakeDatabase(run=run, project=project, workflow=workflow)

    class TemporalMustNotBeRead:
        def __getattr__(self, name):
            raise AssertionError(f"workflow catalog touched Temporal: {name}")

    app = FastAPI()
    app.state.settings = SimpleNamespace()
    app.include_router(router)
    authenticate(app)
    app.state.runtime = SimpleNamespace(
        database=database,
        temporal=TemporalMustNotBeRead(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/workflows")

    assert response.status_code == 200
    assert response.headers["X-Tin-Read-Source"] == "postgres"
    assert response.json()[0]["key"] == "content.design_md"
    assert response.json()[0]["current_commit_sha"] == "d" * 40


@pytest.mark.asyncio
@pytest.mark.parametrize("protected", [False, True])
async def test_catalog_workflow_run_pins_the_definition_commit(protected) -> None:
    project, workflow, run = fixture_state()
    database = FakeDatabase(run=run, project=project, workflow=workflow)

    class FakeTemporal:
        def __init__(self):
            self.started = 0

        async def start_workflow(self, *args, **kwargs):
            self.started += 1

    temporal = FakeTemporal()
    app = FastAPI()
    app.include_router(router)
    authenticate(app)
    app.state.runtime = SimpleNamespace(database=database, temporal=temporal)
    app.state.settings = SimpleNamespace(
        task_queue="test", billing_hosted_defaults_enabled=protected
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/workflows/{workflow.id}/runs",
            json={"project_id": str(project.id)},
        )

    if not protected:
        assert response.status_code == 409
        assert temporal.started == 0
        assert "protected Codex execution" in response.json()["detail"]
        return
    assert response.status_code == 202
    assert response.json()["workflow_id"] == str(workflow.id)
    assert response.json()["definition_commit_sha"] == "d" * 40
    assert temporal.started == 1


@pytest.mark.asyncio
async def test_required_integration_fails_one_visible_run_before_temporal_starts() -> None:
    project, workflow, run = fixture_state()
    workflow = replace(
        workflow,
        definition={
            **workflow.definition,
            "integration_requirements": [
                {
                    "provider_key": "infra.github",
                    "capabilities": ["contents.read"],
                    "required": True,
                }
            ],
        },
    )
    database = FakeDatabase(run=run, project=project, workflow=workflow)

    class TemporalMustNotStart:
        async def start_workflow(self, *args, **kwargs):
            raise AssertionError("integration preflight allowed Temporal to start")

    class Integrations:
        async def ensure_requirements(self, **values):
            assert values["project_id"] == project.id
            raise IntegrationAuthorizationError("Connect GitHub before starting this workflow")

    app = FastAPI()
    app.include_router(router)
    authenticate(app)
    app.state.runtime = SimpleNamespace(
        database=database,
        temporal=TemporalMustNotStart(),
        integrations=Integrations(),
    )
    app.state.settings = SimpleNamespace(task_queue="test", billing_hosted_defaults_enabled=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/workflows/{workflow.id}/runs",
            json={"project_id": str(project.id)},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "Connect GitHub before starting this workflow"
    assert database.run.status == RunStatus.FAILED
    assert database.run.error_message == (
        "Integration unavailable: Connect GitHub before starting this workflow"
    )


@pytest.mark.asyncio
async def test_workflow_start_idempotency_reuses_run_without_restarting_temporal() -> None:
    project, workflow, run = fixture_state()

    class IdempotentDatabase(FakeDatabase):
        async def create_run(self, **values):
            assert values["start_idempotency_key"] == "chat:request-1"
            return self.run, False

    class TemporalMustNotStart:
        async def start_workflow(self, *args, **kwargs):
            raise AssertionError("an idempotent workflow retry restarted Temporal")

    database = IdempotentDatabase(run=run, project=project, workflow=workflow)
    app = FastAPI()
    app.include_router(router)
    authenticate(app)
    app.state.runtime = SimpleNamespace(database=database, temporal=TemporalMustNotStart())
    app.state.settings = SimpleNamespace(task_queue="test", billing_hosted_defaults_enabled=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/workflows/{workflow.id}/runs",
            json={"project_id": str(project.id)},
            headers={"Idempotency-Key": " chat:request-1 "},
        )

    assert response.status_code == 202
    assert response.json()["id"] == str(run.id)


def test_run_rollouts_migration_stores_redacted_gzip_per_file() -> None:
    migration = (Path(__file__).parents[1] / "migrations" / "018_run_rollouts.sql").read_text()

    assert "CREATE TABLE run_rollouts" in migration
    assert "REFERENCES workflow_runs(id) ON DELETE CASCADE" in migration
    assert "content_gzip bytea NOT NULL" in migration
    assert "UNIQUE (run_id, generation, filename)" in migration
    assert "stage IN ('codex', 'codex_procedure', 'project_task')" in migration


def test_chat_history_migration_keeps_one_message_table_and_idempotent_run_key() -> None:
    migration = (Path(__file__).parents[1] / "migrations" / "007_chat_history.sql").read_text()

    assert "CREATE TABLE chat_messages" in migration
    assert "UNIQUE (project_id, request_id, role)" in migration
    assert "CREATE TABLE chat_threads" not in migration
    assert "ADD COLUMN start_idempotency_key" in migration
    assert "workflow_runs_start_idempotency_idx" in migration


@pytest.mark.asyncio
async def test_pending_idempotent_run_retries_temporal_start_safely() -> None:
    project, workflow, running = fixture_state()
    pending = replace(running, status=RunStatus.PENDING)

    class IdempotentDatabase(FakeDatabase):
        async def create_run(self, **values):
            return self.run, False

    class AlreadyStartedTemporal:
        async def start_workflow(self, *args, **kwargs):
            raise WorkflowAlreadyStartedError(
                pending.temporal_workflow_id,
                pending.executor,
                run_id=str(pending.id),
            )

    database = IdempotentDatabase(run=pending, project=project, workflow=workflow)
    app = FastAPI()
    app.include_router(router)
    authenticate(app)
    app.state.runtime = SimpleNamespace(database=database, temporal=AlreadyStartedTemporal())
    app.state.settings = SimpleNamespace(task_queue="test", billing_hosted_defaults_enabled=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/workflows/{workflow.id}/runs",
            json={"project_id": str(project.id)},
            headers={"Idempotency-Key": "chat:request-2"},
        )

    assert response.status_code == 202
    assert response.json()["id"] == str(pending.id)


@pytest.mark.asyncio
async def test_temporal_start_failure_is_projected_without_a_phantom_running_run() -> None:
    project, workflow, run = fixture_state()
    database = FakeDatabase(run=run, project=project, workflow=workflow)

    class UnavailableTemporal:
        async def start_workflow(self, *args, **kwargs):
            raise ConnectionError("Temporal unavailable")

    app = FastAPI()
    app.include_router(router)
    authenticate(app)
    app.state.runtime = SimpleNamespace(database=database, temporal=UnavailableTemporal())
    app.state.settings = SimpleNamespace(task_queue="test", billing_hosted_defaults_enabled=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/workflows/{workflow.id}/runs",
            json={"project_id": str(project.id)},
        )

    assert response.status_code == 502
    assert response.json()["detail"]["run_id"] == str(run.id)
    assert database.run.status == RunStatus.FAILED
    assert database.run.error_message == "TemporalStartError: workflow did not start"
