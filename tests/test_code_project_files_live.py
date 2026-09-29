"""Opt-in file reads through real E2B/storage, with disposable product state.

No model calls, founder approvals or customer files are involved. The test records
the revision at admission, edits a synthetic file, and checks old and new runs.
"""

import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from dotenv import dotenv_values
from temporalio.testing import ActivityEnvironment
from test_private_workflows import ACTOR, fixture
from test_procedure_publication import publication_db as publication_db

from tin_lite.activities import TinActivities
from tin_lite.code_activities import CodeActivities
from tin_lite.code_project_files import source_key
from tin_lite.code_storage import CodeStorage
from tin_lite.e2b_runtime import E2BRuntime
from tin_lite.private_workflows import PackageActivation, PrivateWorkflows
from tin_lite.publication import read_run_output
from tin_lite.run_service import start_workflow_run

MAIN = """import json

def run(ctx, inputs):
    brand = ctx.files.read_text("BRAND.md")
    binary = ctx.files.read_bytes("data/sample.bin")
    escaped = ctx.files.read_text("data/escaped.txt")
    try:
        notes = ctx.files.read_text("missing.md")
    except FileNotFoundError:
        notes = inputs["brand_notes"]
    try:
        ctx.files.read_text("../BRAND.md")
        raise AssertionError("Unsafe path was readable")
    except ValueError:
        pass
    return {
        "path": "reports/BRAND_REFERENCE.md",
        "content": json.dumps({
            "brand": brand, "binary": binary.hex(), "fallback": notes,
            "escaped_bytes": len(escaped.encode("utf-8")),
            "paths": ctx.files.glob("data/*.bin")
        })
    }
"""


@pytest.mark.skipif(
    os.environ.get("TIN_LITE_PROJECT_FILES_LIVE_PROOF") != "1",
    reason="opt-in real E2B/code.storage with disposable local product state",
)
async def test_live_files_are_current_for_new_runs_and_pinned_for_retries(publication_db, tmp_path):
    f = await fixture(publication_db)
    credentials = dotenv_values(os.environ.get("TIN_LITE_PROJECT_FILES_CREDENTIAL_FILE", ".env"))
    storage = CodeStorage(
        organization=credentials.get("TIN_LITE_CODE_STORAGE_ORG") or "tin",
        private_key=credentials["CODE_STORAGE_API_KEY"],
    )
    repo_id = f"projects/{f.project.id}"
    await storage.ensure_repo(repo_id)
    example = Path(__file__).parents[1] / "workflow_packages/example.project_files/workflow.json"
    manifest = json.loads(example.read_text())
    manifest["definition"]["key"] = "custom.project_files_live"
    manifest["definition"]["code"]["timeout_seconds"] = 60
    path = "workflow_packages/custom.project_files_live/workflow.json"
    original = "# Brand\n\nSynthetic original — café.\n"
    revision, _ = await storage.publish_state_documents(
        repo_id=repo_id,
        branch="main",
        documents={
            "BRAND.md": original.encode(),
            "data/sample.bin": bytes([0, 255, 128, 1]),
            # Valid UTF-8 whose JSON-escaped form would exceed the bridge frame.
            "data/escaped.txt": b"\x01" * 63_000,
            path: json.dumps(manifest).encode(),
            "workflow_packages/custom.project_files_live/main.py": MAIN.encode(),
        },
        workflow_key="files.live_fixture",
        execution_key=f"{f.project.id}:initial-files",
        run_id=str(f.project.id),
    )
    await f.db.pool.execute(
        "UPDATE projects SET state_repo_id=$2 WHERE id=$1", f.project.id, repo_id
    )
    f.runtime.storage = storage
    f.settings.e2b_isolated_template = (
        credentials.get("TIN_LITE_E2B_ISOLATED_TEMPLATE") or "tin-lite-codex-isolated"
    )
    private = PrivateWorkflows(database=f.db, storage=storage, settings=f.settings)
    active = await private.activate(
        project_id=f.project.id,
        actor=ACTOR,
        client_id=None,
        selection=PackageActivation(
            path=path, revision=revision, request_id=uuid4(), expected_revision=None
        ),
    )
    workflow = await f.db.get_workflow(UUID(active["workflow_id"]))
    arguments = dict(
        runtime=f.runtime,
        settings=f.settings,
        workflow=workflow,
        project_id=f.project.id,
        started_by_clerk_user_id=ACTOR,
        start_idempotency_key=str(uuid4()),
        input_payload={"brand_notes": "Caller supplied this fallback"},
    )
    first = await start_workflow_run(**arguments)
    assert (await f.db.get_effect(source_key(first.id))).result["revision"] == revision
    assert (await start_workflow_run(**arguments)).id == first.id
    updated = "# Brand\n\nSynthetic newer copy.\n"
    await storage.publish_state_documents(
        repo_id=repo_id,
        branch="main",
        documents={"BRAND.md": updated.encode()},
        workflow_key="files.live_fixture",
        execution_key=f"{first.id}:later-edit",
        run_id=str(first.id),
    )

    class Compute(E2BRuntime):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.created = []

        async def create(self, **kwargs):
            sandbox_id = await super().create(**kwargs)
            self.created.append(sandbox_id)
            return sandbox_id

    compute = Compute(
        api_key=credentials["E2B_API_KEY"],
        template="tin-lite-codex",
        isolated_template=f.settings.e2b_isolated_template,
        timeout_seconds=60,
        egress_allow_hosts=(),
        usage_database=f.db,
    )
    code = CodeActivities(
        common=TinActivities(database=f.db, storage=storage, settings=f.settings, sandboxes=compute)
    )

    async def execute(run, expected):
        await ActivityEnvironment().run(code.execute, str(run.id))
        await ActivityEnvironment().run(code.execute, str(run.id))
        await code.publish(str(run.id))
        await code.publish(str(run.id))
        assert not await code.review(str(run.id))
        await code.project(str(run.id))
        final = await f.db.get_run(run.id)
        assert final.status.value == "succeeded", final.error_message
        output = await read_run_output(storage=storage, run=final, repo_id=repo_id)
        assert json.loads(output.content) == {
            "brand": expected,
            "binary": "00ff8001",
            "escaped_bytes": 63_000,
            "fallback": arguments["input_payload"]["brand_notes"],
            "paths": ["data/sample.bin"],
        }
        assert not await compute.is_running(compute.created[-1])

    try:
        await execute(first, original)
        assert len(compute.created) == 1
        assert (await start_workflow_run(**arguments)).id == first.id
        arguments["start_idempotency_key"] = str(uuid4())
        second = await start_workflow_run(**arguments)
        await execute(second, updated)
        assert len(compute.created) == 2
        proof = {
            "run_ids": [str(first.id), str(second.id)],
            "repo_id": repo_id,
            "private_activation": True,
            "external_services": "real E2B and code.storage; no model calls",
            "product_state": "synthetic files; disposable local DB; mocked Temporal dispatch",
            "admitted_run_read_original_after_edit": True,
            "new_run_read_updated_file": True,
            "binary_and_large_escaped_text_read": True,
            "missing_file_used_caller_fallback": True,
            "repeat_start_and_execution_reused_result": True,
            "sandboxes_deleted": True,
        }
        (tmp_path / "project-files-live-proof.json").write_text(json.dumps(proof, indent=2))
    finally:
        for sandbox_id in compute.created:
            await compute.kill(sandbox_id)
