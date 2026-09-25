import asyncio
import json
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest
from test_content_delivery import approve, configured, fixture, publish_draft, start
from test_private_workflows import ACTOR, app, mcp, structured
from test_procedure_publication import publication_db as publication_db

from tin_lite import content_repository_delivery as delivery
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.organic_audit import canonical_json
from tin_lite.procedures import procedure_checkpoint_path
from tin_lite.run_service import start_workflow_run
from tin_lite.workflow_inputs import WorkflowInputError


def test_repository_workspaces_share_the_gateway_bounds():
    from tin_lite.procedures import validate_codex_procedure_definition

    chosen = next(w for w in BUILTIN_WORKFLOWS if w.key == delivery.KEY)
    definition, _ = chosen.definition_and_resource_files()
    spec = validate_codex_procedure_definition(definition)
    for workflow in BUILTIN_WORKFLOWS:
        if workflow.executor == "codex.procedure":
            current, _ = workflow.definition_and_resource_files()
            assert "limits" not in current["procedure"]["workspace"]
    # Stored definitions that still pin their former limits parse to the same contract.
    for limits in (
        {"max_files": 1000, "max_bytes": 100_000_000},
        {"max_files": 1000, "max_bytes": 20_000_000},
    ):
        older = deepcopy(definition)
        older["procedure"]["workspace"]["limits"] = limits
        assert validate_codex_procedure_definition(older) == spec


async def prepared(db, monkeypatch, *, approved=True):
    f = await configured(await fixture(db, monkeypatch))
    # This article predates delivery. Do not give its approval a new consequence.
    f.inputs["delivery"] = "draft_only"
    source = await start(f)
    source, f.context, f.raw = await publish_draft(f, source)
    f.source = await approve(f, source) if approved else source
    spec = next(w for w in BUILTIN_WORKFLOWS if w.key == delivery.KEY)
    definition, resources = spec.definition_and_resource_files()
    await db.upsert_registry_workflow(
        workflow_id=spec.id,
        key=spec.key,
        title=spec.title,
        description=spec.description,
        executor=spec.executor,
        definition_repo_id="registry/workflows",
        definition_path=spec.definition_path,
        current_commit_sha="d" * 40,
        version_label=spec.version_label,
        definition=definition,
    )
    read = f.storage.read_canonical_artifact

    async def read_resource(**kw):
        if kw["repo_id"] == "registry/workflows" and kw["commit_sha"] == "d" * 40:
            return (
                canonical_json(definition)
                if kw["path"] == spec.definition_path
                else resources[kw["path"]]
            )
        return await read(**kw)

    f.storage.read_canonical_artifact = read_resource

    async def publish(**kwargs):
        return f.storage.repo.edit({kwargs["path"]: kwargs["content"]}), True

    f.storage.publish_state_document = publish
    f.delivery_workflow = await db.get_workflow(spec.id)
    f.delivery_inputs = {"source_run_id": str(f.source.id), "expected_repository": "owner/site"}
    return f


async def deliver_start(f, *, key=None, inputs=None, quote_id=None):
    return await start_workflow_run(
        runtime=f.runtime,
        settings=f.settings,
        workflow=f.delivery_workflow,
        project_id=f.project.id,
        started_by_clerk_user_id=ACTOR,
        input_payload=inputs or f.delivery_inputs,
        start_idempotency_key=key,
        billing_quote_id=quote_id,
    )


async def test_existing_approved_article_atomic_binding_and_duplicate_starts(
    publication_db, monkeypatch
):
    f = await prepared(publication_db, monkeypatch)
    key = str(uuid4())
    run = await deliver_start(f, key=key)
    saved = await delivery.saved_source(f.db, run.id)
    assert saved["source_revision"] == f.source.canonical_commit_sha
    assert saved["article"].startswith("# Useful buyer task")
    assert "Verification notes" not in saved["article"]
    assert saved["binding"]["repository"] == "owner/site"
    assert (await deliver_start(f, key=key)).id == run.id
    with pytest.raises(WorkflowInputError, match="already has a delivery attempt"):
        await deliver_start(f, key=str(uuid4()))
    assert (await f.db.get_run(f.source.id)).status == f.source.status
    assert (await f.db.get_run(f.source.id)).canonical_commit_sha == f.source.canonical_commit_sha
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM workflow_runs WHERE workflow_id=$1", delivery.WORKFLOW_ID
        )
        == 1
    )


async def test_approval_and_project_boundary_before_dispatch(publication_db, monkeypatch):
    f = await prepared(publication_db, monkeypatch, approved=False)
    f.runtime.temporal.start_workflow.reset_mock()
    with pytest.raises(WorkflowInputError, match="approve"):
        await deliver_start(f)
    with pytest.raises(WorkflowInputError, match="from this project"):
        await deliver_start(f, inputs={**f.delivery_inputs, "source_run_id": str(uuid4())})
    f.runtime.temporal.start_workflow.assert_not_called()
    assert not await f.db.pool.fetchval(
        "SELECT id FROM workflow_runs WHERE workflow_id=$1", delivery.WORKFLOW_ID
    )


async def test_article_delivered_by_its_approval_choice_is_not_adapted_again(
    publication_db, monkeypatch
):
    f = await prepared(publication_db, monkeypatch, approved=False)
    # No pinned delivery: the reviewer picks a PR for this one article at approval.
    await f.delivery.choose(run=f.source, mode="github_pr", actor=ACTOR)
    f.source = await approve(f, f.source)
    await f.delivery.deliver(f.source.id)
    f.runtime.integrations.github_create_pull_request.assert_awaited_once()
    facts = await f.service.programs.facts(f.configured.id)
    assert facts["drafts"][f.context["item"]["id"]]["delivery"]["pull_request"]["number"] == 42
    with pytest.raises(WorkflowInputError, match="already has automatic delivery"):
        await deliver_start(f)
    f.runtime.integrations.github_create_pull_request.assert_awaited_once()
    assert not await f.db.pool.fetchval(
        "SELECT id FROM workflow_runs WHERE workflow_id=$1", delivery.WORKFLOW_ID
    )


async def test_concurrent_starts_do_not_purchase_twice(publication_db, monkeypatch):
    f = await prepared(publication_db, monkeypatch)
    results = await asyncio.gather(
        deliver_start(f, key=str(uuid4())),
        deliver_start(f, key=str(uuid4())),
        return_exceptions=True,
    )
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert sum(isinstance(r, WorkflowInputError) for r in results) == 1


async def test_exact_markdown_or_component_copy_not_edited_prose(publication_db, monkeypatch):
    f = await prepared(publication_db, monkeypatch)
    run = await deliver_start(f)
    source = await delivery.saved_source(f.db, run.id)
    manifest = {
        "repository": "owner/site",
        "head_sha": f.binding.head_sha,
        "files": [{"path": "posts/article.md", "content": source["article"]}],
    }
    assert delivery.validate_copy(manifest, source)["article_path"] == "posts/article.md"
    component = (
        "const article = "
        + json.dumps(source["article"])
        + ";\nexport default () => <Markdown>{article}</Markdown>;"
    )
    manifest["files"] = [{"path": "web/src/app/blog/article/page.tsx", "content": component}]
    assert delivery.validate_copy(manifest, source)["copy_check"] == "exact_source_preserved"
    altered = deepcopy(manifest)
    altered["files"][0]["content"] = component.replace("mechanism", "trick")
    with pytest.raises(ValueError, match="unchanged"):
        delivery.validate_copy(altered, source)
    altered = deepcopy(manifest)
    altered["files"].append({"path": "package.json", "content": "{}"})
    with pytest.raises(ValueError, match="dependencies"):
        delivery.validate_copy(altered, source)
    with pytest.raises(ValueError, match="pinned"):
        delivery.validate_copy({**manifest, "repository": "other/site"}, source)


async def test_mcp_contract_and_http_start_share_source_guard(publication_db, monkeypatch):
    f = await prepared(publication_db, monkeypatch)
    server = mcp(f, monkeypatch)
    contract = structured(
        await server.call_tool(
            "get_workflow", {"project_id": str(f.project.id), "workflow_key": delivery.KEY}
        )
    )
    assert "Do not approve" in contract["preparation"]["instruction"]
    assert "preparation.articles" in contract["preparation"]["instruction"]
    assert "get_content_draft_sources" not in contract["preparation"]["instruction"]
    assert contract["id"] == str(delivery.WORKFLOW_ID)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        response = await client.post(
            f"/api/workflows/{delivery.WORKFLOW_ID}/runs",
            json={"project_id": str(f.project.id), "inputs": f.delivery_inputs},
            headers={"Idempotency-Key": str(uuid4())},
        )
    assert response.status_code == 202, response.text
    rows = await delivery.delivery_history(
        f.db.pool, project_id=f.project.id, source_ids=[str(f.source.id)]
    )
    assert rows[str(f.source.id)]["run_id"] == response.json()["id"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f, "outsider")), base_url="https://tin.test"
    ) as client:
        denied = await client.post(
            f"/api/workflows/{delivery.WORKFLOW_ID}/runs",
            json={"project_id": str(f.project.id), "inputs": f.delivery_inputs},
        )
    assert denied.status_code == 404


async def test_mcp_start_reuses_the_same_run_and_discovery_has_titles(publication_db, monkeypatch):
    f = await prepared(publication_db, monkeypatch)
    server = mcp(f, monkeypatch)
    args = {
        "project_id": str(f.project.id),
        "workflow_id": delivery.KEY,
        "inputs": f.delivery_inputs,
        "request_id": str(uuid4()),
    }
    first = structured(await server.call_tool("start_workflow", args))
    repeated = structured(await server.call_tool("start_workflow", args))
    assert first["id"] == repeated["id"]
    sources = await delivery.discover(f.db, f.project.id)
    assert sources["articles"] == [
        {"run_id": str(f.source.id), "title": f.context["item"]["title"]}
    ]


async def checkpoint(f, run):
    await f.activities.prepare_codex_procedure(str(run.id))
    await f.activities.create_codex_procedure_sandbox(str(run.id))
    run = await f.db.get_run(run.id)
    source = await delivery.saved_source(f.db, run.id)
    manifest = {
        "repository": "owner/site",
        "default_branch": "main",
        "head_sha": f.binding.head_sha,
        "title": "Content: reviewed article",
        "body": "Unmerged article. Build not run.",
        "files": [{"path": "posts/article.md", "content": source["article"]}],
        "verification": [delivery.CHECK_COMMAND],
    }
    path = procedure_checkpoint_path(run.id)
    head = f.storage.repo.head
    revision = f.storage.repo.edit({path: canonical_json(manifest)}, parent=run.expected_head_sha)
    f.storage.branch_revision, f.storage.repo.head = revision, head
    key = f"{run.id}:procedure_artifact_persist"
    async with f.db.effect_lock(key, "procedure_artifact_persist") as (conn, _):
        await f.db.start_effect(conn, execution_key=key, operation="procedure_artifact_persist")
        await f.db.complete_procedure_persist(
            conn,
            execution_key=key,
            run_id=run.id,
            result={
                "ephemeral_commit_sha": revision,
                "checkpoint_path": path,
                "sandbox_killed": True,
            },
            sandbox_killed=True,
        )
    return await f.db.get_run(run.id)


async def test_delivery_commit_and_durable_receipt_are_idempotent(publication_db, monkeypatch):
    f = await prepared(publication_db, monkeypatch)
    run = await checkpoint(f, await deliver_start(f))
    for _ in range(2):
        await f.activities.commit_codex_procedure_artifact(str(run.id))
    f.runtime.integrations.github_create_pull_request.assert_awaited_once()
    call = f.runtime.integrations.github_create_pull_request.call_args.kwargs
    assert call["expected_binding"] == f.binding
    assert call["execution_key"] == f"{run.id}:procedure_pull_request"
    proof = await f.db.get_effect(f"{run.id}:procedure_canonical_commit")
    assert proof.result["external_url"] == "https://github.com/owner/site/pull/42"
    content = await f.storage.read_canonical_artifact(
        repo_id=f.project.state_repo_id,
        commit_sha=proof.result["canonical_commit_sha"],
        path=proof.result["artifact_path"],
    )
    assert b"does not prove rendered equivalence" in content
    assert b"git diff --check" in content
    status = await delivery.retry_status(f.db, await f.db.get_run(run.id))
    assert status["status"] == "completed"
    assert status["pull_request"]["url"] == proof.result["external_url"]


async def test_failed_delivery_reconciles_frozen_patch_without_compute(publication_db, monkeypatch):
    from tin_lite.content_delivery_api import retry_delivery

    f = await prepared(publication_db, monkeypatch)
    run = await checkpoint(f, await deliver_start(f))
    await f.db.project_failure(run_id=run.id, error_message="Synthetic provider outage")
    run = await f.db.get_run(run.id)
    result = await retry_delivery(
        runtime=f.runtime, settings=f.settings, project_id=f.project.id, run_id=run.id
    )
    assert result["status"] == "pending"
    assert f.runtime.temporal.start_workflow.call_args.args[0] == "tin.content_draft_delivery"
    for _ in range(2):
        await delivery.recover_delivery(
            database=f.db, storage=f.storage, integrations=f.runtime.integrations, run=run
        )
    f.runtime.integrations.github_create_pull_request.assert_awaited_once()
    assert (await f.db.get_run(run.id)).status.value == "failed"  # History is not rewritten.
    rows = await delivery.delivery_history(
        f.db.pool, project_id=f.project.id, source_ids=[str(f.source.id)]
    )
    assert rows[str(f.source.id)]["pull_request_url"] == "https://github.com/owner/site/pull/42"
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM activity_events "
            "WHERE run_id=$1 AND event_type='content_delivery_recovered'",
            run.id,
        )
        == 1
    )


async def test_worker_recovery_keeps_immutable_checkpoint_before_delivery(
    publication_db, monkeypatch
):
    f = await prepared(publication_db, monkeypatch)
    run = await checkpoint(f, await deliver_start(f))
    persisted = await f.db.get_effect(f"{run.id}:procedure_artifact_persist")
    expected = persisted.result["ephemeral_commit_sha"]
    # The sandbox pushed the checkpoint, then the worker lost its acknowledgment.
    await f.db.pool.execute(
        "DELETE FROM effect_receipts WHERE execution_key=$1", f"{run.id}:procedure_artifact_persist"
    )
    await f.activities.persist_codex_procedure_artifact(str(run.id))
    recovered = await f.db.get_effect(f"{run.id}:procedure_artifact_persist")
    assert recovered.result["ephemeral_commit_sha"] == expected
    assert recovered.result["reconciled_from_ephemeral_branch"] is True
    await f.activities.commit_codex_procedure_artifact(str(run.id))
    f.runtime.integrations.github_create_pull_request.assert_awaited_once()


async def test_explicit_failed_adaptation_retry_is_not_delivery_retry(publication_db, monkeypatch):
    f = await prepared(publication_db, monkeypatch)
    original = await deliver_start(f)
    with pytest.raises(WorkflowInputError, match="Reconcile"):
        await deliver_start(f, inputs={**f.delivery_inputs, "retry_run_id": str(original.id)})
    await f.db.project_failure(run_id=original.id, error_message="Failed before producing a patch")
    with pytest.raises(ValueError, match="no saved repository patch"):
        await delivery.retry_status(f.db, await f.db.get_run(original.id))
    repeated = await deliver_start(
        f, inputs={**f.delivery_inputs, "retry_run_id": str(original.id)}
    )
    assert repeated.id != original.id
    assert (await delivery.saved_source(f.db, repeated.id))["source_run_id"] == str(f.source.id)


async def test_delivery_funds_one_api_session_without_quote_approval(publication_db, monkeypatch):
    from tin_lite.billing import BillingService
    from tin_lite.billing_contracts import ProjectSpendingPolicy, object_value

    f = await prepared(publication_db, monkeypatch)
    f.settings.billing_test_enabled = True
    f.settings.codex_api_projects = {f.project.id}
    f.db.billing = BillingService(database=f.db, settings=f.settings)
    await f.db.pool.execute(
        "UPDATE workspaces SET created_by_clerk_user_id=$2 WHERE id=$1",
        f.project.workspace_id,
        ACTOR,
    )
    await f.db.pool.execute(
        "INSERT INTO workspace_memberships(workspace_id,clerk_user_id) "
        "VALUES($1,$2) ON CONFLICT DO NOTHING",
        f.project.workspace_id,
        ACTOR,
    )
    await f.db.billing.enroll_test(f.project.workspace_id, ACTOR)
    await f.db.billing.update_policy(
        f.project.id,
        ACTOR,
        ProjectSpendingPolicy(
            per_run_nanos=10_000_000_000,
            monthly_nanos=20_000_000_000,
            concurrency=2,
            expected_revision=0,
        ),
    )
    # Synthetic test funds; no Stripe or provider network calls in this test.
    await f.db.pool.execute(
        "UPDATE billing_accounts SET balance_nanos=$2 WHERE workspace_id=$1",
        f.project.workspace_id,
        20_000_000_000,
    )
    key = str(uuid4())
    run = await deliver_start(f, key=key)
    assert (await deliver_start(f, key=key)).id == run.id
    assert (
        await f.db.pool.fetchval("SELECT count(*) FROM billing_run_budgets WHERE run_id=$1", run.id)
        == 1
    )
    terms = object_value(
        await f.db.pool.fetchval("SELECT terms FROM billing_run_budgets WHERE run_id=$1", run.id)
    )
    assert terms["kind"] == "codex_api" and terms["funding"] == "procedure_session_v1"
    assert terms["codex_contract"]["protocol"] == "tin-codex-api-v4"
    assert await f.db.pool.fetchval("SELECT reserved_nanos FROM billing_accounts") == 5_000_000_000
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_quotes") == 0
