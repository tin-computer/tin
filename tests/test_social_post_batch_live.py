"""Opt-in real social drafting with isolated local product state and synthetic sources.

Uses the configured E2B, code.storage and model accounts. Never connects to the
product database, modifies a customer project, posts, or deploys the service.
"""

import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from dotenv import dotenv_values
from openai import AsyncOpenAI
from pydantic import SecretStr
from temporalio.testing import ActivityEnvironment
from temporalio.worker import Replayer, Worker
from test_code_article_sources import fixture
from test_private_workflows import ACTOR, app
from test_procedure_publication import publication_db as publication_db
from test_project_codex_execution import temporal_env as temporal_env

from tin_lite.activities import TinActivities
from tin_lite.code_activities import CodeActivities
from tin_lite.code_models import registered_routes
from tin_lite.code_storage import CodeStorage
from tin_lite.codex_execution import ProjectCodexExecution
from tin_lite.domain import RunStatus
from tin_lite.e2b_runtime import E2BRuntime
from tin_lite.model_providers import ModelRouter, OpenAIModelProvider, ProviderName
from tin_lite.model_usage import ModelUsageRecorder
from tin_lite.public_workflows import PUBLIC_WORKFLOWS
from tin_lite.publication import read_run_output
from tin_lite.run_service import start_workflow_run
from tin_lite.workflows import CodeWorkflow
from tin_lite.writing_style import STYLE_PATH

ARTICLE_BODY = """A useful analytics chart begins with data that can be checked.
The CSV import keeps each row tied to its original transaction. A teammate can
inspect a chart's underlying row and compare it with the source record before
using the chart in a decision.

## Make rejected rows visible

The import validates each row before adding it to the chart. A missing transaction
date or an amount that cannot be parsed produces a validation message for that
row. Invalid rows stay out of the chart and remain visible in the import report.
The report states which rows were accepted and which need correction. It does not
silently replace a missing amount with zero.

## Keep the correction connected to its source

When a row fails validation, the import report gives its original row number and
the field that needs attention. The person preparing the CSV can correct that
field in the source file, then import the corrected file. This makes the source
file the place to resolve the problem, instead of editing chart totals by hand.

## Explain the limits

Passing validation establishes that the row has the expected fields and usable
values. It does not establish that the transaction happened or that the data is
complete. Those questions still need a source record and the person responsible
for it. The chart keeps that distinction visible: usable data is a starting point
for a decision, not proof that every business assumption is correct.
"""


@pytest.mark.skipif(
    os.environ.get("TIN_LITE_SOCIAL_LIVE_PROOF") != "1",
    reason="opt-in real E2B/code.storage and one paid social drafting model call",
)
async def test_live_social_article_to_review(publication_db, temporal_env, monkeypatch, tmp_path):
    f = await fixture(publication_db, monkeypatch)
    credentials = dotenv_values(os.environ.get("TIN_LITE_SOCIAL_CREDENTIAL_FILE", ".env"))
    storage = CodeStorage(
        organization=credentials.get("TIN_LITE_CODE_STORAGE_ORG") or "tin",
        private_key=credentials["CODE_STORAGE_API_KEY"],
    )
    # Seed ordinary project files only in this disposable schema/repository.
    repo_id = f"projects/{f.project.id}"
    await storage.ensure_repo(repo_id)
    article_path = "content/articles/live-example.md"
    article = "# Traceable analytics imports\n\n" + ARTICLE_BODY
    style = b"# Writing style\nUse plain, concrete sentences. Avoid hype and exclamation marks.\n"
    path = "workflow_packages/social.post_batch/workflow.json"
    root = Path(__file__).parents[1]
    manifest = json.loads((root / path).read_text())
    definition = manifest["definition"]
    files = {
        article_path: article.encode(),
        STYLE_PATH: style,
        path: (root / path).read_bytes(),
        "workflow_packages/social.post_batch/main.py": (
            root / "workflow_packages/social.post_batch/main.py"
        ).read_bytes(),
    }
    revision, _ = await storage.publish_state_documents(
        repo_id=repo_id,
        branch="main",
        documents=files,
        workflow_key="social.live_fixture",
        execution_key=f"{f.project.id}:live-fixture",
        run_id=str(f.project.id),
    )
    await f.db.pool.execute(
        "UPDATE projects SET state_repo_id=$2 WHERE id=$1", f.project.id, repo_id
    )
    selected = next(w for w in PUBLIC_WORKFLOWS if w.key == definition["key"])
    await f.db.upsert_registry_workflow(
        workflow_id=selected.id,
        key=selected.key,
        title=definition["title"],
        description=definition["description"],
        executor="workflow.code",
        definition_repo_id=repo_id,
        definition_path=path,
        current_commit_sha=revision,
        version_label=definition["version"],
        definition=definition,
    )

    class Compute(E2BRuntime):
        sandbox_ids = []

        async def create(self, **kwargs):
            sandbox = await super().create(**kwargs)
            self.sandbox_ids.append(sandbox)
            return sandbox

    compute = Compute(
        api_key=credentials["E2B_API_KEY"],
        template="tin-lite-codex",
        isolated_template=credentials.get("TIN_LITE_E2B_ISOLATED_TEMPLATE")
        or "tin-lite-codex-isolated",
        timeout_seconds=60,
        egress_allow_hosts=(),
        usage_database=f.db,
    )
    calls = []

    async def count_request(request):
        calls.append(request.url.path)

    model_key = credentials["TIN_LITE_LUNA_API_KEY"]
    client = AsyncOpenAI(
        api_key=model_key,
        max_retries=0,
        timeout=25,
        http_client=httpx.AsyncClient(event_hooks={"request": [count_request]}),
    )
    router = ModelRouter(
        providers={ProviderName.OPENAI: OpenAIModelProvider(api_key=model_key, client=client)},
        routes=registered_routes(),
        recorder=ModelUsageRecorder(f.db),
    )
    f.settings.luna_api_key = SecretStr(model_key)
    f.runtime.storage = storage
    f.runtime.temporal = temporal_env.client
    f.runtime.model_router = router
    common = TinActivities(database=f.db, storage=storage, settings=f.settings, sandboxes=compute)
    code = CodeActivities(common=common, model_router=router)
    try:
        async with Worker(
            temporal_env.client,
            task_queue=f.settings.task_queue,
            workflows=[CodeWorkflow, ProjectCodexExecution],
            activities=[
                common.resolve_codex_project,
                code.execute,
                code.publish,
                code.review,
                code.approve,
                code.project,
                code.failure,
            ],
            graceful_shutdown_timeout=timedelta(seconds=1),
        ):
            workflow = await f.db.get_workflow(selected.id)
            request_id = str(uuid4())
            arguments = dict(
                runtime=f.runtime,
                settings=f.settings,
                workflow=workflow,
                project_id=f.project.id,
                started_by_clerk_user_id=ACTOR,
                start_idempotency_key=request_id,
                input_payload={},
            )
            run = await start_workflow_run(**arguments)
            assert (await start_workflow_run(**arguments)).id == run.id
            handle = temporal_env.client.get_workflow_handle(run.temporal_workflow_id)
            async with asyncio.timeout(180):
                while True:
                    run = await f.db.get_run(run.id)
                    if run.status in {RunStatus.NEEDS_INPUT, RunStatus.FAILED, RunStatus.SUCCEEDED}:
                        break
                    await asyncio.sleep(0.25)
            usage = await f.db.pool.fetch(
                "SELECT result FROM effect_receipts WHERE operation='native_model_usage_v1' "
                "AND result->>'run_id'=$1",
                str(run.id),
            )
            proof = {
                "run_id": str(run.id),
                "repo_id": repo_id,
                "status": run.status.value,
                "supplier_calls": len(calls),
                "source": f"synthetic project file {article_path}",
                "product_state": "disposable local Postgres and Temporal; synthetic identity",
                "external_services": "real E2B, code.storage and OpenAI",
                "usage": [json.loads(r["result"]) for r in usage],
            }
            (tmp_path / "live-proof.json").write_text(json.dumps(proof, indent=2))
            assert run.status == RunStatus.NEEDS_INPUT, run.error_message
            output = await read_run_output(storage=storage, run=run, repo_id=repo_id)
            (tmp_path / "social-drafts.md").write_bytes(output.content)
            (tmp_path / "source-article.md").write_bytes(article.encode())
            assert output.content.count(b"Source excerpt from the article:") == 4
            assert len(calls) == 1
            assert len(compute.sandbox_ids) == 1
            assert not await compute.is_running(compute.sandbox_ids[0])
            # Duplicate activity delivery cannot regenerate, republish, or purchase.
            await ActivityEnvironment().run(code.execute, str(run.id))
            await code.publish(str(run.id))
            assert len(calls) == 1
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
            ) as http:
                approved = await http.post(f"/api/workflows/runs/{run.id}/approve", json={})
                assert approved.status_code == 202, approved.text
            await asyncio.wait_for(handle.result(), 30)
            history = await handle.fetch_history()
            await Replayer(workflows=[CodeWorkflow, ProjectCodexExecution]).replay_workflow(history)
            final = await f.db.get_run(run.id)
            assert final.status == RunStatus.SUCCEEDED
            proof.update(
                status=final.status.value,
                artifact_revision=final.canonical_commit_sha,
                artifact_path=final.artifact_path,
                sandbox_deleted=True,
                repeat_start_same_run=True,
                duplicate_activity_no_purchase=True,
                review="synthetic local HTTP approval",
                temporal_replay=True,
            )
            (tmp_path / "live-proof.json").write_text(json.dumps(proof, indent=2))
            print(f"Live proof and synthetic drafts: {tmp_path}")
    finally:
        for sandbox_id in compute.sandbox_ids:
            await compute.kill(sandbox_id)
        await router.close()
