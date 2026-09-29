"""Project-file reads use the admission snapshot, not a mutable branch or sandbox key."""

import base64
import io
import json
from uuid import UUID, uuid4

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from temporalio.testing import ActivityEnvironment
from test_billing import billed as billed
from test_private_workflows import ACTOR
from test_procedure_publication import publication_db as publication_db
from test_workflow_code import (
    OUTPUT,
    CheckpointStorage,
    SyntheticCompute,
    activate_code,
    setup,
    start,
)

from tin_lite import code_project_files, code_runner
from tin_lite.code_activities import CodeActivities


class NoCache:
    def get(self, _key):
        return None

    def put(self, _key, _value, _weight):
        pass


class FileStorage(CheckpointStorage):
    def __init__(self):
        super().__init__()
        self._pinned = NoCache()


class FileCompute(SyntheticCompute):
    def __init__(self):
        super().__init__()
        self.observed = []

    async def run_code_and_kill(self, *, packet, model_call, **kwargs):
        assert packet["model_client"] is True
        text = base64.b64decode(
            await model_call({"kind": "file", "operation": "read_text", "path": "notes/one.md"})
        ).decode("utf-8")
        encoded = await model_call(
            {"kind": "file", "operation": "read_bytes", "path": "notes/one.md"}
        )
        paths = await model_call({"kind": "file", "operation": "glob", "path": "notes/*.md"})
        assert paths == ["notes/one.md"]
        assert encoded
        self.observed.append(text)
        with pytest.raises(code_project_files.CodeProjectFileError, match="file_not_found"):
            await model_call({"kind": "file", "operation": "read_text", "path": "notes/none.md"})
        with pytest.raises(code_project_files.CodeProjectFileError, match="invalid_file_path"):
            await model_call({"kind": "file", "operation": "read_text", "path": "../other.md"})
        self.calls += 1
        return json.dumps({"path": OUTPUT, "content": f"# Pinned note\n{text}"}).encode()


def test_worker_file_api_decodes_control_heavy_text_and_missing_fallback(monkeypatch):
    requests = []
    replies = iter(
        [
            {"result": base64.b64encode(('"\n' * 32_000).encode()).decode()},
            {"result": base64.b64encode(b"\x00\xff").decode()},
            {"result": ["notes/one.md"]},
            {"error": "file_not_found"},
        ]
    )

    class Socket:
        def __init__(self, *_args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def settimeout(self, _seconds):
            pass

        def connect(self, _path):
            pass

        def sendall(self, raw):
            requests.append(json.loads(raw))

        def makefile(self, _mode):
            return io.BytesIO(json.dumps(next(replies)).encode() + b"\n")

    monkeypatch.setattr(code_runner.socket, "socket", Socket)
    files = code_runner.Context({}).files
    assert files.read_text("notes/one.md") == '"\n' * 32_000
    assert files.read_bytes("notes/raw.bin") == b"\x00\xff"
    assert files.glob("notes/*.md") == ["notes/one.md"]
    with pytest.raises(FileNotFoundError, match="notes/none.md"):
        files.read_text("notes/none.md")
    assert [request["operation"] for request in requests] == [
        "read_text",
        "read_bytes",
        "glob",
        "read_text",
    ]


def test_glob_matches_path_components_like_a_filesystem():
    match = code_project_files._glob_matches
    assert match("reports/one.md", "reports/*.md")
    assert not match("reports/nested/one.md", "reports/*.md")
    assert match("reports/one.md", "reports/**/*.md")
    assert match("reports/nested/one.md", "reports/**/*.md")
    assert not match("other/one.md", "reports/**/*.md")


async def test_new_run_sees_latest_files_and_retry_keeps_its_revision(billed, monkeypatch):
    f = billed
    storage, compute = FileStorage(), FileCompute()
    server, common, code = await setup(f, monkeypatch, storage=storage, compute=compute)
    active = await activate_code(f, server)
    old_revision = storage.repo.edit({"notes/one.md": b"Old project fact\n"})
    request_id = str(uuid4())
    first = await start(f, server, active, request_id=request_id)
    first_run = await f.db.get_run(UUID(first["id"]))
    first_source = await code_project_files.saved_source(f.db, first_run, f.project)
    assert first_source["revision"] == old_revision
    new_revision = storage.repo.edit({"notes/one.md": b"New project fact\n"})
    assert (await start(f, server, active, request_id=request_id))["id"] == first["id"]
    assert await code_project_files.saved_source(f.db, first_run, f.project) == first_source
    second = await start(f, server, active)
    second_run = await f.db.get_run(UUID(second["id"]))
    second_source = await code_project_files.saved_source(f.db, second_run, f.project)
    assert second_source["revision"] == new_revision
    await ActivityEnvironment().run(code.execute, first["id"])
    storage.branch_revision = None
    await ActivityEnvironment().run(code.execute, second["id"])
    assert compute.observed == ["Old project fact\n", "New project fact\n"]
    assert compute.calls == 2
    assert not await f.db.pool.fetchval(
        "SELECT execution_key FROM effect_receipts WHERE operation='code_model_call_v1'"
    )
    assert not await f.db.pool.fetchval("SELECT id FROM billing_operations")


async def test_legacy_admitted_run_has_no_file_bridge(billed, monkeypatch):
    f = billed

    class LegacyCompute(SyntheticCompute):
        async def run_code_and_kill(self, *, packet, **kwargs):
            assert "model_client" not in packet
            assert "model_call" not in kwargs
            self.calls += 1
            return json.dumps(self.result).encode()

    storage = FileStorage()
    compute = LegacyCompute()
    server, common, _code = await setup(f, monkeypatch, storage=storage, compute=compute)
    active = await activate_code(f, server)
    workflow = await f.db.get_workflow(UUID(active["workflow_id"]))
    run, created = await f.db.create_run(
        project_id=f.project.id,
        workflow_id=workflow.id,
        started_by_clerk_user_id=ACTOR,
        input_payload={"minimum_cents": 1000},
        pinned_definition=workflow.definition,
        definition_commit_sha=workflow.current_commit_sha,
    )
    assert created
    assert await code_project_files.saved_source(f.db, run, f.project) is None
    await ActivityEnvironment().run(CodeActivities(common=common).execute, str(run.id))
    assert compute.calls == 1


async def test_unavailable_head_fails_before_run_and_budget(billed, monkeypatch):
    f = billed
    storage = FileStorage()
    server, _common, _code = await setup(f, monkeypatch, storage=storage)
    active = await activate_code(f, server)

    async def unavailable(_repo, _branch):
        return None

    monkeypatch.setattr(storage, "head_sha", unavailable)
    before_budgets = await f.db.pool.fetchval("SELECT count(*) FROM billing_run_budgets")
    with pytest.raises(ToolError, match="canonical revision"):
        await start(f, server, active)
    assert not await f.db.pool.fetchval(
        "SELECT id FROM workflow_runs WHERE workflow_id=$1", UUID(active["workflow_id"])
    )
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_run_budgets") == before_budgets
    assert f.runtime.sandboxes.calls == 0


async def test_foreign_revision_and_nonregular_or_oversized_file_are_rejected(billed, monkeypatch):
    f = billed
    storage = FileStorage()
    server, _common, _code = await setup(f, monkeypatch, storage=storage)
    active = await activate_code(f, server)
    source = await code_project_files.select(
        database=f.db, storage=storage, project_id=f.project.id
    )
    other = await f.db.create_project(name="Other", state_repo_id="projects/other-code-files")
    workflow = await f.db.get_workflow(UUID(active["workflow_id"]))
    with pytest.raises(ValueError, match="revision is invalid"):
        await f.db.create_run(
            project_id=f.project.id,
            workflow_id=UUID(active["workflow_id"]),
            pinned_definition=workflow.definition,
            definition_commit_sha=workflow.current_commit_sha,
            code_project_files_source={**source, "project_id": str(other.id)},
        )
    revision = storage.repo.edit(
        {"notes/huge.txt": b"A" * 64_001, "notes/link.txt": ("120000", b"target")}
    )
    with pytest.raises(ValueError, match="bounded regular"):
        await storage.read_bounded_project_file(
            repo_id=f.project.state_repo_id,
            commit_sha=revision,
            path="notes/huge.txt",
            max_bytes=64_000,
        )
    with pytest.raises(ValueError, match="bounded regular"):
        await storage.read_bounded_project_file(
            repo_id=f.project.state_repo_id,
            commit_sha=revision,
            path="notes/link.txt",
            max_bytes=64_000,
        )
