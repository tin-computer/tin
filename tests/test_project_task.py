from __future__ import annotations

import base64
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import get_type_hints
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from tin_lite.activities import TinActivities
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.code_storage import CodeStorage, reviewed_task_diff
from tin_lite.domain import Project, RunStatus, WorkflowRun
from tin_lite.e2b_runtime import E2BRuntime, SandboxTaskEvent, SandboxTaskInput
from tin_lite.luna import LunaService

ROOT = Path(__file__).parents[1]
TASK_WORKFLOW_ID = UUID("00000000-0000-4000-8000-000000000006")
TASK_RAW_DIFF = (
    "diff --git a/report.md b/report.md\n--- /dev/null\n+++ b/report.md\n@@ -0,0 +1 @@\n+# Report\n"
)


def task_catalog_workflow() -> dict:
    task = next(item for item in BUILTIN_WORKFLOWS if item.key == "project.task")
    return {
        "id": str(task.id),
        "project_id": None,
        "key": task.key,
        "title": task.title,
        "description": task.description,
        "executor": task.executor,
        "version_label": task.version_label,
        "current_commit_sha": "d" * 40,
        "definition": task.definition,
        "status": "active",
    }


def test_project_task_is_one_explicit_hidden_registry_definition() -> None:
    tasks = [item for item in BUILTIN_WORKFLOWS if item.key == "project.task"]
    assert len(tasks) == 1
    task = tasks[0]
    assert task.id == TASK_WORKFLOW_ID
    assert task.definition["kind"] == "task"
    assert task.definition["input_schema"]["required"] == [
        "project_id",
        "instruction",
        "title",
    ]


def test_project_task_turn_temporal_payload_contains_only_strings() -> None:
    hints = get_type_hints(TinActivities.run_project_task_turn)

    assert hints["payload"] == dict[str, str]


def test_task_bridge_enables_environment_protocol_only_for_isolated_api_tasks() -> None:
    source = (ROOT / "sandbox" / "task_app_server.py").read_text()

    assert '"experimentalApi": ISOLATED' in source
    assert '"environment/add"' in source
    assert "features.hooks=false" in source
    assert "features.remote_plugin=false" in source
    assert "runtimeWorkspaceRoots" not in source


@pytest.mark.asyncio
async def test_luna_forwards_project_task_instruction_without_a_privileged_path() -> None:
    project_id = uuid4()

    class Responses:
        def __init__(self) -> None:
            self.calls = 0

        async def create(self, payload):
            self.calls += 1
            if self.calls == 1:
                return {
                    "id": "resp_route",
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "call_task",
                            "name": "start_project_task",
                            "arguments": json.dumps(
                                {
                                    "project_id": str(project_id),
                                    "instruction": (
                                        "Inspect the navigation and fix the broken link."
                                    ),
                                    "title": "Fix navigation link",
                                }
                            ),
                        }
                    ],
                }
            return {
                "id": "resp_done",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Task started."}],
                    }
                ],
            }

        async def close(self):
            pass

    class WorkflowApi:
        arguments = None

        async def list_workflows(self, project_id, *, authorization):
            return [task_catalog_workflow()]

        async def get_project_memory(self, project_id, *, authorization):
            return {"content": "# Project memory"}

        async def list_project_runs(self, project_id, *, authorization):
            return []

        async def start_workflow(self, workflow_id, **values):
            assert workflow_id == TASK_WORKFLOW_ID
            self.arguments = values["arguments"]
            return {
                "id": str(uuid4()),
                "project_id": str(project_id),
                "workflow_id": str(workflow_id),
                "workflow_name": "project.task",
                "status": "pending",
            }

        async def close(self):
            pass

    workflow_api = WorkflowApi()
    luna = LunaService(responses=Responses(), workflow_api=workflow_api)  # type: ignore[arg-type]
    result = await luna.respond(
        project_id=project_id,
        message="Please inspect the navigation and fix its broken link.",
        authorization="Bearer member",
    )

    assert result.routed_workflow_key == "project.task"
    assert workflow_api.arguments == {
        "instruction": "Inspect the navigation and fix the broken link.",
        "title": "Fix navigation link",
    }


@pytest.mark.asyncio
async def test_task_runtime_turns_file_changes_into_review(monkeypatch) -> None:
    payload = {
        "outcome": "completed",
        "summary": "Updated the navigation.",
        "message": "The broken link is fixed and ready for review.",
        "question": None,
    }
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    event_payload = base64.b64encode(
        json.dumps({"sequence": 1, "kind": "turn_started", "message": "Codex started."}).encode()
    ).decode()

    class Handle:
        async def wait(self):
            return SimpleNamespace(
                stdout=(
                    "TIN_TASK_EVENT=working\n"
                    "TIN_TASK_STEERED=00000000-0000-4000-8000-000000000010\n"
                    "TIN_TASK_HAS_CHANGES=1\n"
                    f"TIN_TASK_RESULT={encoded}\n"
                )
            )

        async def send_stdin(self, value):
            del value

    class Commands:
        async def run(self, *args, **kwargs):
            if args[0].endswith("isolated-procedure check"):
                return SimpleNamespace(stdout="TIN_ISOLATION_READY_V1")
            if args[0].endswith("run-task --check-api"):
                return SimpleNamespace(stdout="TIN_TASK_API_READY_V1")
            assert args == ("/opt/tin-lite/run-task",)
            assert kwargs["background"] is True
            assert kwargs["stdin"] is True
            assert "OPENAI_API_KEY" not in kwargs["envs"]
            await kwargs["on_stdout"](f"TIN_TASK_EVENT={event_payload}\n")
            return Handle()

    class Sandbox:
        commands = Commands()
        killed = False

        async def kill(self):
            self.killed = True

    sandbox = Sandbox()

    async def connect(*args, **kwargs):
        return sandbox

    monkeypatch.setattr("tin_lite.e2b_runtime.AsyncSandbox.connect", connect)
    runtime = E2BRuntime(
        api_key="test",
        template="tin-lite-codex",
        timeout_seconds=60,
        egress_allow_hosts=(),
    )
    events: list[SandboxTaskEvent] = []

    async def record_event(event: SandboxTaskEvent) -> None:
        events.append(event)

    result = await runtime.run_task_and_kill(
        sandbox_id="sandbox-1",
        run_input=SandboxTaskInput(
            execution_key="run:turn:1",
            isolated=True,
            api_url="https://tin.test/relay",
            api_grant="synthetic",
            canonical_url="https://storage.test/canonical.git",
            canonical_auth_header="Authorization: Basic canonical",
            canonical_branch="main",
            ephemeral_url="https://storage.test/ephemeral.git",
            ephemeral_auth_header="Authorization: Basic ephemeral",
            ephemeral_branch="tasks/run/1",
            proxy_url="http://proxy.test:3128",
            no_proxy="tin.test",
            run_id="run-1",
            context={"instruction": "Fix it", "transcript": []},
        ),
        on_event=record_event,
    )

    assert result.outcome == "review"
    assert result.has_changes is True
    assert result.delivered_entry_ids == ("00000000-0000-4000-8000-000000000010",)
    assert events == [SandboxTaskEvent(1, "turn_started", "Codex started.")]
    assert sandbox.killed is True


@pytest.mark.asyncio
async def test_task_diff_is_bounded_and_bound_to_exact_bytes() -> None:
    raw = "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n"

    class Repo:
        async def list_commits(self, **values):
            raise AssertionError("the moving canonical branch is not the review base")

        async def get_commit_diff(self, **values):
            assert values["sha"] == "c" * 40
            assert values["base_sha"] == "a" * 40
            return {
                "sha": "c" * 40,
                "stats": {"files": 1, "additions": 1, "deletions": 1, "changes": 2},
                "files": [
                    {
                        "path": "app.py",
                        "old_path": None,
                        "state": "modified",
                        "raw_state": "M",
                        "raw": raw,
                        "bytes": len(raw),
                        "is_eof": True,
                    }
                ],
                "filtered_files": [],
            }

    storage = object.__new__(CodeStorage)

    async def get_repo(repo_id):
        assert repo_id == "projects/test"
        return Repo()

    storage.get_repo = get_repo  # type: ignore[method-assign]
    storage.procedure_checkpoint_revision = AsyncMock(return_value="c" * 40)
    projection, exact = await storage.get_task_branch_diff(
        repo_id="projects/test",
        branch="tasks/run/1",
        base_branch="main",
        expected_base_sha="a" * 40,
    )

    assert exact == raw
    assert projection["files"][0]["path"] == "app.py"
    assert projection["sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert projection["base_sha"] == "a" * 40
    assert projection["source_revision"] == "c" * 40


@pytest.mark.asyncio
async def test_task_diff_rejects_a_missing_checkpoint() -> None:
    storage = object.__new__(CodeStorage)
    storage.get_repo = AsyncMock(return_value=SimpleNamespace())
    storage.procedure_checkpoint_revision = AsyncMock(return_value=None)
    with pytest.raises(RuntimeError, match="checkpoint is unavailable"):
        await storage.get_task_branch_diff(
            repo_id="projects/test",
            branch="tasks/run/1",
            base_branch="main",
            expected_base_sha="a" * 40,
        )


def test_reviewed_task_diff_rebuilds_only_the_approved_bytes() -> None:
    raw = TASK_RAW_DIFF
    task_diff = {
        "files": [{"path": "report.md", "patch": raw}],
        "sha256": hashlib.sha256(raw.encode()).hexdigest(),
    }

    assert reviewed_task_diff(task_diff) == raw
    task_diff["files"][0]["patch"] = raw.replace("Report", "Changed")
    with pytest.raises(RuntimeError, match="approval hash"):
        reviewed_task_diff(task_diff)


@pytest.mark.asyncio
async def test_task_approval_applies_exact_reviewed_diff_to_latest_project_head() -> None:
    run_id = uuid4()
    project = Project(uuid4(), "Tin POC", "projects/test", "main")
    raw = TASK_RAW_DIFF
    task_diff = {
        "files": [{"path": "report.md", "patch": raw}],
        "sha256": hashlib.sha256(raw.encode()).hexdigest(),
    }
    run = WorkflowRun(
        id=run_id,
        project_id=project.id,
        workflow_id=uuid4(),
        executor="project.task",
        definition_commit_sha="d" * 40,
        temporal_workflow_id=f"project.task:{run_id}",
        thread_id="thread",
        generation=1,
        fencing_token=7,
        status=RunStatus.RUNNING,
        sandbox_id="sandbox-1",
        lease_owner=f"{run_id}:project_task",
        lease_active=True,
        ephemeral_branch=f"tasks/{run_id}/1",
        expected_head_sha="a" * 40,
        task_phase="applying",
        task_diff=task_diff,
        task_has_changes=True,
    )

    class Database:
        def __init__(self) -> None:
            self.completed_sha = None
            self.effect_result = None
            self.deferred = False

        @asynccontextmanager
        async def effect_lock(self, execution_key, operation):
            assert execution_key == f"{run_id}:task_apply"
            assert operation == "project_task_apply"
            yield object(), None

        async def start_effect(self, *args, **kwargs):
            pass

        async def get_run(self, value):
            return run if value == run_id else None

        async def get_project(self, value):
            return project if value == project.id else None

        async def validate_lease(self, **values):
            assert values["fencing_token"] == 7
            return True

        @asynccontextmanager
        async def project_state_lock(self, *args):
            yield

        async def complete_task_approval(self, *, run_id, canonical_commit_sha):
            assert run_id == run.id
            self.completed_sha = canonical_commit_sha

        async def complete_effect(self, *args, **values):
            self.effect_result = values["result"]

        async def fail_effect(self, *args, **kwargs):
            raise AssertionError("successful apply must not fail its receipt")

        async def defer_task_approval(self, **values):
            self.deferred = True

    class Storage:
        def __init__(self) -> None:
            self.apply_values = None

        async def get_repo(self, repo_id):
            assert repo_id == project.state_repo_id
            return object()

        async def head_sha(self, repo, branch):
            assert branch == project.canonical_branch
            return "b" * 40

        async def apply_task_diff(self, **values):
            self.apply_values = values
            return "c" * 40

    database = Database()
    storage = Storage()
    activities = object.__new__(TinActivities)
    activities._db = database
    activities._storage = storage

    outcome = await activities.apply_project_task_changes(str(run_id))

    assert outcome == "applied"
    assert storage.apply_values["expected_head_sha"] == "b" * 40
    assert storage.apply_values["raw_diff"] == raw
    assert storage.apply_values["expected_diff_sha256"] == task_diff["sha256"]
    assert storage.apply_values["original_base_sha"] == "a" * 40
    assert storage.apply_values["reviewed_files"] == task_diff["files"]
    assert database.completed_sha == "c" * 40
    assert database.effect_result == {"canonical_commit_sha": "c" * 40}
    assert database.deferred is False


@pytest.mark.asyncio
async def test_task_apply_failure_returns_to_review_without_losing_the_proposal() -> None:
    run_id = uuid4()
    project = Project(uuid4(), "Tin POC", "projects/test", "main")
    raw = TASK_RAW_DIFF
    run = WorkflowRun(
        id=run_id,
        project_id=project.id,
        workflow_id=uuid4(),
        executor="project.task",
        definition_commit_sha="d" * 40,
        temporal_workflow_id=f"project.task:{run_id}",
        thread_id="thread",
        generation=1,
        fencing_token=7,
        status=RunStatus.RUNNING,
        sandbox_id="sandbox-1",
        lease_owner=f"{run_id}:project_task",
        lease_active=True,
        ephemeral_branch=f"tasks/{run_id}/1",
        expected_head_sha="a" * 40,
        task_phase="applying",
        task_diff={
            "files": [{"path": "report.md", "patch": raw}],
            "sha256": hashlib.sha256(raw.encode()).hexdigest(),
        },
        task_has_changes=True,
    )

    class Database:
        def __init__(self) -> None:
            self.failed = False
            self.deferred = False

        @asynccontextmanager
        async def effect_lock(self, *args):
            yield object(), None

        async def start_effect(self, *args, **kwargs):
            pass

        async def get_run(self, value):
            return run if value == run_id else None

        async def get_project(self, value):
            return project if value == project.id else None

        async def validate_lease(self, **values):
            return True

        @asynccontextmanager
        async def project_state_lock(self, *args):
            yield

        async def fail_effect(self, *args, **kwargs):
            self.failed = True

        async def defer_task_approval(self, **values):
            assert values["run_id"] == run_id
            self.deferred = True

    class Storage:
        async def get_repo(self, repo_id):
            return object()

        async def head_sha(self, repo, branch):
            return "b" * 40

        async def apply_task_diff(self, **values):
            raise RuntimeError("patch conflicts with the latest project state")

    database = Database()
    activities = object.__new__(TinActivities)
    activities._db = database
    activities._storage = Storage()

    outcome = await activities.apply_project_task_changes(str(run_id))

    assert outcome == "retry"
    assert database.failed is True
    assert database.deferred is True


def test_project_task_migration_enforces_one_active_task_and_separate_transcript() -> None:
    migration = (ROOT / "migrations" / "008_project_task.sql").read_text()
    assert "CREATE UNIQUE INDEX one_active_project_task" in migration
    assert "executor = 'project.task'" in migration
    assert "CREATE TABLE project_task_entries" in migration
    assert "('pending', 'running', 'needs_input', 'paused')" in migration
    assert "workflow_versions" not in migration


def test_project_task_projection_types_nullable_question_parameter() -> None:
    source = (ROOT / "src" / "tin_lite" / "db.py").read_text()
    assert "task_question = $5::text" in source
    assert "WHEN $5::text IS NULL THEN NULL ELSE now()" in source


def test_task_ui_is_separate_from_luna_chat_and_hides_the_definition_from_registry() -> None:
    script = (ROOT / "src" / "tin_lite" / "static" / "app.js").read_text()
    assert 'workflow.definition?.kind !== "task"' in script
    assert "function openTask(runId)" in script
    assert "function renderTask()" in script
    assert 'data-task-action="pause"' in script
    assert 'data-task-action="approve"' in script
    assert "File changes stay isolated until you approve the exact diff." in script
    assert 'data-open-task="${escapeHtml(run.id)}"' in script
    assert 'entry.kind === "event"' in script
    assert 'class="composer-field" id="task-message"' in script
    assert "!queuedRunIds.has(run.id)" in script
    assert "function openTaskDocument(runId, path)" in script
    assert 'data-task-document="${escapeHtml(file.path)}"' in script
    assert "View exact diff" in script
    assert "Approval accepted. Applying the exact reviewed changes." in script
    assert 'data-activity-task="${escapeHtml(run.id)}"' in script
    assert 'eyebrow: "changes preserved"' in script
    workflow_source = (ROOT / "src" / "tin_lite" / "workflows.py").read_text()
    assert "self._approved or self._resume or self._stop" in workflow_source
    database_source = (ROOT / "src" / "tin_lite" / "db.py").read_text()
    assert "WHEN task_phase = 'review' THEN NULL" in database_source
    bridge_source = (ROOT / "sandbox" / "task_app_server.py").read_text()
    assert "reconcile the latest canonical" in bridge_source
