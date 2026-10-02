from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from tin_lite.activities import TinActivities
from tin_lite.api import router
from tin_lite.auth import AuthContext, require_user
from tin_lite.catalog import PROJECT_MEMORY_WORKFLOW_ID
from tin_lite.domain import (
    MEMORY_INDEX_PATH,
    EffectReceipt,
    Project,
    RunStatus,
    WorkflowRun,
)
from tin_lite.memory import MemoryGardener, MemorySource
from tin_lite.skills import load_skill_suite

ROOT = Path(__file__).parents[1]


class FakeDatabase:
    def __init__(self, *, project: Project, run: WorkflowRun, source: WorkflowRun) -> None:
        self.project = project
        self.run = run
        self.source = source
        self.receipts: dict[str, EffectReceipt] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.projection_writes = 0
        self.events: list[str] = []

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

    @asynccontextmanager
    async def project_state_lock(self, conn, project_id: UUID) -> AsyncIterator[None]:
        assert project_id == self.project.id
        yield

    async def complete_effect(self, conn, *, execution_key: str, result: dict) -> None:
        operation = self.receipts[execution_key].operation
        self.receipts[execution_key] = EffectReceipt(execution_key, operation, "completed", result)

    async def fail_effect(self, conn, *, execution_key: str, error_message: str) -> None:
        operation = self.receipts[execution_key].operation
        self.receipts[execution_key] = EffectReceipt(execution_key, operation, "failed", None)

    async def get_run(self, run_id: UUID):
        return self.run if run_id == self.run.id else None

    async def get_project(self, project_id: UUID):
        return self.project if project_id == self.project.id else None

    async def mark_run_running(self, run_id: UUID) -> None:
        assert run_id == self.run.id
        self.run = replace(self.run, status=RunStatus.RUNNING)

    async def list_memory_source_runs(self, **values) -> list[WorkflowRun]:
        assert values["project_id"] == self.project.id
        assert values["exclude_run_id"] == self.run.id
        return [self.source]

    async def get_effect(self, execution_key: str, conn=None):
        return self.receipts.get(execution_key)

    async def project_memory_success(self, **values) -> None:
        self.projection_writes += 1
        self.project = replace(
            self.project,
            memory_commit_sha=values["canonical_commit_sha"],
            memory_index_path=values["artifact_path"],
            memory_index=values["memory_index"],
        )
        self.run = replace(
            self.run,
            status=RunStatus.SUCCEEDED,
            canonical_commit_sha=values["canonical_commit_sha"],
            artifact_ref=values["artifact_ref"],
            artifact_path=values["artifact_path"],
        )

    async def add_activity(self, *, event_type: str, **values) -> None:
        self.events.append(event_type)


class FakeStorage:
    def __init__(self, memory_index: bytes) -> None:
        self.memory_index = memory_index
        self.publishes = 0

    async def read_canonical_artifact(self, *, path: str, **values) -> bytes:
        if path == MEMORY_INDEX_PATH:
            return self.memory_index
        return b"# Product design\n\nDurable architecture facts.\n"

    async def publish_state_document(self, *, content: bytes, **values) -> tuple[str, bool]:
        self.publishes += 1
        self.memory_index = content
        await asyncio.sleep(0)
        return "m" * 40, True


class FakeGardener:
    def __init__(self, source_ref: str) -> None:
        self.calls = 0
        self.source_ref = source_ref

    async def garden(self, **values) -> bytes:
        self.calls += 1
        assert values["sources"][0].artifact_ref == self.source_ref
        return (
            "# Test memory\n\n"
            "## Architecture\n\n- Durable architecture facts.\n\n"
            "## Sources\n\n"
            f"- content.design_md · {self.source_ref}\n"
        ).encode()


def memory_fixture() -> tuple[Project, WorkflowRun, WorkflowRun]:
    project = Project(uuid4(), "Test", "projects/test", "main")
    source_id = uuid4()
    source = WorkflowRun(
        id=source_id,
        project_id=project.id,
        workflow_id=UUID("00000000-0000-4000-8000-000000000001"),
        executor="content.design_md",
        definition_commit_sha="d" * 40,
        temporal_workflow_id=f"content.design_md:{source_id}",
        thread_id="design",
        generation=1,
        fencing_token=1,
        status=RunStatus.SUCCEEDED,
        canonical_commit_sha="c" * 40,
        artifact_path="DESIGN.md",
        artifact_ref="code.storage://projects/test@canonical/DESIGN.md",
    )
    run_id = uuid4()
    run = WorkflowRun(
        id=run_id,
        project_id=project.id,
        workflow_id=PROJECT_MEMORY_WORKFLOW_ID,
        executor="project.memory",
        definition_commit_sha="e" * 40,
        temporal_workflow_id=f"project.memory:{run_id}",
        thread_id=str(PROJECT_MEMORY_WORKFLOW_ID),
        generation=1,
        fencing_token=2,
        status=RunStatus.PENDING,
    )
    return project, run, source


@pytest.mark.asyncio
async def test_project_memory_duplicate_execution_creates_one_commit_and_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, run, source = memory_fixture()
    database = FakeDatabase(project=project, run=run, source=source)
    gardener = FakeGardener(source.artifact_ref)
    storage = FakeStorage(b"")
    heartbeats: list[dict] = []
    monkeypatch.setattr("tin_lite.activities.activity.heartbeat", heartbeats.append)
    activities = TinActivities(
        database=database,  # type: ignore[arg-type]
        storage=storage,  # type: ignore[arg-type]
        sandboxes=SimpleNamespace(),
        settings=SimpleNamespace(),
        memory_gardener=gardener,  # type: ignore[arg-type]
    )

    await asyncio.gather(
        activities.garden_project_memory(str(run.id)),
        activities.garden_project_memory(str(run.id)),
    )
    await asyncio.gather(
        activities.project_memory_result(str(run.id)),
        activities.project_memory_result(str(run.id)),
    )

    assert gardener.calls == 1
    assert storage.publishes == 1
    assert database.projection_writes == 1
    assert database.run.status == RunStatus.SUCCEEDED
    assert database.project.memory_index_path == MEMORY_INDEX_PATH
    assert database.events == ["memory_gardened", "project_memory_updated"]
    assert heartbeats == [{"stage": "memory_gardener"}]


@pytest.mark.asyncio
async def test_memory_gardener_loads_ordered_skill_suite_and_requires_provenance() -> None:
    source = MemorySource(
        run_id=uuid4(),
        workflow_key="content.design_md",
        artifact_ref="code.storage://projects/test@abc/DESIGN.md",
        content="# Design\n",
    )

    class FakeResponses:
        def __init__(self) -> None:
            self.payload = None

        async def create(self, payload: dict) -> dict:
            self.payload = payload
            return {
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    "# Test memory\n\n## Facts\n\n- One fact.\n\n"
                                    f"## Sources\n\n- {source.artifact_ref}"
                                ),
                            }
                        ],
                    }
                ]
            }

    responses = FakeResponses()
    skill_suite = load_skill_suite(ROOT / "workflow_skills" / "project-memory")
    gardener = MemoryGardener(responses=responses, skill_suite=skill_suite)

    result = await gardener.garden(project_name="Test", sources=[source])

    assert source.artifact_ref.encode() in result
    assert responses.payload["store"] is False
    assert skill_suite.index("# Safe project-memory gardener") < skill_suite.index(
        "# Read-only inventory"
    )
    assert skill_suite.index("# Read-only inventory") < skill_suite.index("# Provenance review")


@pytest.mark.asyncio
async def test_project_memory_endpoint_reads_only_postgres_projection() -> None:
    project, _, _ = memory_fixture()
    project = replace(
        project,
        memory_commit_sha="m" * 40,
        memory_index_path=MEMORY_INDEX_PATH,
        memory_index="# Test memory\n",
    )

    class FakeProjectDatabase:
        async def get_project(self, project_id: UUID):
            return project if project_id == project.id else None

        async def has_project_access(self, *, project_id: UUID, clerk_user_id: str) -> bool:
            return project_id == project.id and clerk_user_id == "user_test"

    class MustNotBeRead:
        def __getattr__(self, name):
            raise AssertionError(f"memory endpoint touched an external store: {name}")

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id="user_test",
        token_type="session_token",  # noqa: S106
        session_id="sess_test",
    )
    app.state.runtime = SimpleNamespace(
        database=FakeProjectDatabase(),
        temporal=MustNotBeRead(),
        storage=MustNotBeRead(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/projects/{project.id}/memory")

    assert response.status_code == 200
    assert response.headers["X-Tin-Read-Source"] == "postgres"
    assert response.json()["content"] == "# Test memory\n"


def test_memory_sources_keep_the_newest_that_fit_one_gardener_call() -> None:
    from tin_lite.memory import MAX_MEMORY_BYTES, MAX_SOURCE_BYTES, newest_sources

    # Both reach one model's context whole, so each stays within ~200k tokens.
    assert MAX_SOURCE_BYTES == MAX_MEMORY_BYTES == 700_000

    def source(name: str, size: int) -> MemorySource:
        return MemorySource(
            run_id=uuid4(),
            workflow_key="content.generate",
            artifact_ref=f"code.storage://projects/test@abc/{name}.md",
            content="x" * size,
        )

    oldest, too_large, middle, newest = (
        source("oldest", 300_000),
        source("too-large", 900_000),
        source("middle", 300_000),
        source("newest", 300_000),
    )
    # d711b841 failed outright at 200,000 bytes; older sources now drop out instead.
    assert newest_sources([oldest, too_large, middle, newest]) == [middle, newest]
    assert newest_sources([middle, newest]) == [middle, newest]
    with pytest.raises(ValueError, match="memory sources exceed 700000 bytes"):
        newest_sources([too_large])
