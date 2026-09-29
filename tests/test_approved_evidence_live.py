"""Opt-in evidence transport through real E2B/storage, with disposable product state.

The approved producer is a synthetic fixture, not a live model or founder approval.
Only one short code sandbox runs; no model calls, posting, or customer project changes.
"""

import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from dotenv import dotenv_values
from temporalio.testing import ActivityEnvironment
from test_code_evidence import fixture
from test_private_workflows import ACTOR
from test_procedure_publication import publication_db as publication_db

from tin_lite.activities import TinActivities
from tin_lite.code_activities import CodeActivities
from tin_lite.code_storage import CodeStorage
from tin_lite.e2b_runtime import E2BRuntime
from tin_lite.private_workflows import PackageActivation, PrivateWorkflows
from tin_lite.publication import read_run_output
from tin_lite.run_service import start_workflow_run


@pytest.mark.skipif(
    os.environ.get("TIN_LITE_EVIDENCE_LIVE_PROOF") != "1",
    reason="opt-in real E2B/code.storage with isolated local product state",
)
async def test_live_private_consumer_preserves_selected_copy(publication_db, monkeypatch, tmp_path):
    f = await fixture(publication_db, monkeypatch)
    credentials = dotenv_values(os.environ.get("TIN_LITE_EVIDENCE_CREDENTIAL_FILE", ".env"))
    storage = CodeStorage(
        organization=credentials.get("TIN_LITE_CODE_STORAGE_ORG") or "tin",
        private_key=credentials["CODE_STORAGE_API_KEY"],
    )
    repo_id = f"projects/{f.project.id}"
    await storage.ensure_repo(repo_id)
    producer = await f.db.get_workflow(f.posts.workflow_id)
    root = Path(__file__).parents[1]
    example = root / "workflow_packages/example.approved_evidence"
    manifest = json.loads((example / "workflow.json").read_text())
    manifest["definition"]["key"] = "custom.evidence_live"
    manifest["definition"]["code"]["evidence"]["posts"]["workflow_key"] = producer.key
    path = "workflow_packages/custom.evidence_live/workflow.json"
    raw = await f.storage.read_canonical_artifact(
        repo_id=f.project.state_repo_id,
        commit_sha=f.posts.canonical_commit_sha,
        path=f.posts.artifact_path,
    )
    revision, _ = await storage.publish_state_documents(
        repo_id=repo_id,
        branch="main",
        documents={
            producer.definition_path: json.dumps(
                {"package_format": "tin-workflow-package-v1", "definition": producer.definition}
            ).encode(),
            f.posts.artifact_path: raw,
            path: json.dumps(manifest).encode(),
            "workflow_packages/custom.evidence_live/main.py": (example / "main.py").read_bytes(),
        },
        workflow_key="evidence.live_fixture",
        execution_key=f"{f.posts.id}:live-fixture",
        run_id=str(f.posts.id),
    )
    # Move only the synthetic fixture and its proof to this new real repository.
    await f.db.pool.execute(
        "UPDATE projects SET state_repo_id=$2 WHERE id=$1", f.project.id, repo_id
    )
    await f.db.pool.execute(
        "UPDATE workflows SET definition_repo_id=$2 WHERE id=$1", producer.id, repo_id
    )
    await f.db.pool.execute(
        "UPDATE workflow_runs SET canonical_commit_sha=$2, definition_commit_sha=$2, "
        "expected_head_sha=$2 WHERE id=$1",
        f.posts.id,
        revision,
    )
    publication_key = f"{f.posts.id}:procedure_canonical_commit"
    publication = (await f.db.get_effect(publication_key)).result
    publication["canonical_commit_sha"] = revision
    publication["checkpoint"].update(
        definition_commit_sha=revision, source_base_sha=revision, ephemeral_commit_sha=revision
    )
    await f.db.pool.execute(
        "UPDATE effect_receipts SET result=$2::jsonb WHERE execution_key=$1",
        publication_key,
        json.dumps(publication),
    )
    f.runtime.storage = storage
    # Enroll only the disposable fixture in this in-process test configuration.
    f.settings.private_workflow_projects = {f.project.id}
    f.settings.e2b_isolated_template = (
        credentials.get("TIN_LITE_E2B_ISOLATED_TEMPLATE") or "tin-lite-codex-isolated"
    )
    private = PrivateWorkflows(database=f.db, storage=storage, settings=f.settings)
    activated = await private.activate(
        project_id=f.project.id,
        actor=ACTOR,
        client_id=None,
        selection=PackageActivation(
            path=path, revision=revision, request_id=uuid4(), expected_revision=None
        ),
    )
    workflow = await f.db.get_workflow(UUID(activated["workflow_id"]))
    arguments = dict(
        runtime=f.runtime,
        settings=f.settings,
        workflow=workflow,
        project_id=f.project.id,
        started_by_clerk_user_id=ACTOR,
        start_idempotency_key=str(uuid4()),
        input_payload={"posts_run_id": str(f.posts.id)},
    )
    run = await start_workflow_run(**arguments)
    assert (await start_workflow_run(**arguments)).id == run.id
    await storage.publish_state_documents(
        repo_id=repo_id,
        branch="main",
        documents={f.posts.artifact_path: b"# Later edited copy\n"},
        workflow_key="evidence.live_fixture",
        execution_key=f"{run.id}:later-edit",
        run_id=str(run.id),
    )

    class Compute(E2BRuntime):
        created = []

        async def create(self, **kwargs):
            sandbox_id = await super().create(**kwargs)
            self.created.append(sandbox_id)
            return sandbox_id

    compute = Compute(
        api_key=credentials["E2B_API_KEY"],
        template="tin-lite-codex",
        isolated_template=credentials.get("TIN_LITE_E2B_ISOLATED_TEMPLATE")
        or "tin-lite-codex-isolated",
        timeout_seconds=60,
        egress_allow_hosts=(),
        usage_database=f.db,
    )
    code = CodeActivities(
        common=TinActivities(database=f.db, storage=storage, settings=f.settings, sandboxes=compute)
    )
    try:
        env = ActivityEnvironment()
        await env.run(code.execute, str(run.id))
        await env.run(code.execute, str(run.id))
        await code.publish(str(run.id))
        await code.publish(str(run.id))
        assert not await code.review(str(run.id))
        await code.project(str(run.id))
        final = await f.db.get_run(run.id)
        assert final.status.value == "succeeded", final.error_message
        output = await read_run_output(storage=storage, run=final, repo_id=repo_id)
        assert raw in output.content and b"Later edited copy" not in output.content
        assert revision.encode() in output.content
        assert len(compute.created) == 1
        assert not await compute.is_running(compute.created[0])
        proof = {
            "run_id": str(run.id),
            "repo_id": repo_id,
            "status": final.status.value,
            "source": "synthetic approved code output",
            "private_activation": True,
            "product_state": "disposable local Postgres; synthetic identity; mocked dispatch",
            "external_services": "real E2B and code.storage; no model calls",
            "source_snapshot_survives_file_edit": True,
            "sandbox_deleted": True,
            "duplicate_start_and_execution": "one run and one sandbox",
            "artifact_revision": final.canonical_commit_sha,
        }
        (tmp_path / "live-proof.json").write_text(json.dumps(proof, indent=2))
        (tmp_path / "output.md").write_bytes(output.content)
        print(f"Live evidence proof: {tmp_path}")
    finally:
        for sandbox_id in compute.created:
            await compute.kill(sandbox_id)
