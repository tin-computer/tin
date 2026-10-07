"""Public packages use the existing Registry and code runtime, not private activation."""

import json
import runpy
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import jsonschema
import pytest
from temporalio.testing import ActivityEnvironment
from test_billing import billed as billed
from test_billing import fund
from test_code_models import sdk_router
from test_community_packages import stage
from test_private_workflows import ACTOR, app, structured
from test_procedure_publication import publication_db as publication_db
from test_registry_recipe_publication import WIKI, RegistrySnapshots, catalog_database
from test_workflow_code import CheckpointStorage, SyntheticCompute, setup, start

from tin_lite import catalog, public_workflows
from tin_lite.code_activities import CodeActivities
from tin_lite.code_models import model_terms, request_contract
from tin_lite.community import REPOSITORY_ROOT
from tin_lite.domain import RunStatus
from tin_lite.private_workflows import PrivateWorkflowError, require_private_execution
from tin_lite.procedures import load_pinned_codex_procedure
from tin_lite.public_workflows import PublicWorkflow
from tin_lite.publication import read_run_output
from tin_lite.workflow_code import load_code_package, validate_code_definition, validate_code_result


class PublishedSnapshots(RegistrySnapshots):
    async def read_workflow_resource(self, *, commit_sha, path, **_):
        return self.revisions[commit_sha][path]

    read_canonical_artifact = read_workflow_resource


def select(monkeypatch, *keys):
    entries = tuple(PublicWorkflow(uuid4(), key) for key in keys)
    monkeypatch.setattr(public_workflows, "PUBLIC_WORKFLOWS", entries)
    # These intentionally partial catalogs do not include the X parent or the organic system,
    # nor the package children each publishes with it.
    monkeypatch.setattr(
        catalog,
        "BUILTIN_WORKFLOWS",
        tuple(
            item
            for item in catalog.BUILTIN_WORKFLOWS
            if item.key not in {"social.x_draft", "organic.traffic_system"}
        ),
    )
    return entries


async def test_unselected_packages_never_enter_the_registry(monkeypatch):
    select(monkeypatch)
    db, storage = catalog_database(), PublishedSnapshots()
    await catalog.sync_builtin_workflows(database=db, storage=storage, system_wiki=WIKI)
    assert {row.key for row in db.rows.values()} == {w.key for w in catalog.BUILTIN_WORKFLOWS}
    assert await public_workflows.load_public_workflows() == ()


async def test_public_code_publication_preserves_manifest_resources_and_old_revision(monkeypatch):
    key = "example.csv_summary"
    (entry,) = select(monkeypatch, key)
    db, storage = catalog_database(), PublishedSnapshots()
    await catalog.sync_builtin_workflows(database=db, storage=storage, system_wiki=WIKI)
    row = db.rows[entry.id]
    assert row.project_id is None and row.executor == "workflow.code"
    assert row.definition_repo_id == "registry/workflows"
    assert row.definition_path == f"workflow_packages/{key}/workflow.json"
    first = row.current_commit_sha
    manifest = json.loads(storage.revisions[first][row.definition_path])
    assert manifest["package_format"] == "tin-workflow-package-v1"
    first_package = await load_code_package(
        storage=storage,
        repo_id=row.definition_repo_id,
        commit_sha=first,
        definition_path=row.definition_path,
    )
    assert first_package[0] == row.definition
    assert (
        first_package[2]["main.py"]
        == (REPOSITORY_ROOT / f"workflow_packages/{key}/main.py").read_bytes()
    )
    await catalog.sync_builtin_workflows(database=db, storage=storage, system_wiki=WIKI)
    assert db.rows[entry.id].current_commit_sha == first

    (package,) = await public_workflows.load_public_workflows()
    package.definition["title"] = "A changed title"
    manifest["definition"]["title"] = "A changed title"
    package.files[package.definition_path] = json.dumps(manifest).encode()
    monkeypatch.setattr(catalog, "load_public_workflows", AsyncMock(return_value=(package,)))
    await catalog.sync_builtin_workflows(database=db, storage=storage, system_wiki=WIKI)
    assert db.rows[entry.id].current_commit_sha != first
    assert (
        await load_code_package(
            storage=storage,
            repo_id=row.definition_repo_id,
            commit_sha=first,
            definition_path=row.definition_path,
        )
        == first_package
    )


async def test_public_codex_package_uses_the_same_publication_path(tmp_path, monkeypatch):
    key = "research.contributed_digest"
    stage(tmp_path, key)
    (entry,) = select(monkeypatch, key)
    monkeypatch.setattr(public_workflows, "REPOSITORY_ROOT", tmp_path)
    db, storage = catalog_database(), PublishedSnapshots()
    await catalog.sync_builtin_workflows(database=db, storage=storage, system_wiki=WIKI)
    row = db.rows[entry.id]
    procedure = await load_pinned_codex_procedure(
        storage=storage,
        repo_id=row.definition_repo_id,
        commit_sha=row.current_commit_sha,
        definition_path=row.definition_path,
    )
    assert procedure.workflow_key == key


@pytest.mark.parametrize(
    "problem", ["missing", "duplicate_key", "duplicate_id", "builtin_key", "builtin_id"]
)
async def test_selection_errors_fail_before_any_catalog_writes(monkeypatch, problem):
    (entry,) = select(monkeypatch, "example.csv_summary")
    if problem == "missing":
        select(monkeypatch, "example.missing")
    elif problem == "duplicate_key":
        select(monkeypatch, entry.key, entry.key)
    elif problem == "duplicate_id":
        monkeypatch.setattr(
            public_workflows,
            "PUBLIC_WORKFLOWS",
            (
                entry,
                PublicWorkflow(entry.id, "example.feedback_digest"),
            ),
        )
    else:
        # Use a valid package but collide with a native identity before publication.
        (package,) = await public_workflows.load_public_workflows()
        from dataclasses import replace

        builtin = catalog.BUILTIN_WORKFLOWS[0]
        package = replace(
            package,
            **{
                "key" if problem == "builtin_key" else "id": builtin.key
                if problem == "builtin_key"
                else builtin.id
            },
        )
        monkeypatch.setattr(catalog, "load_public_workflows", AsyncMock(return_value=(package,)))
    db, storage = catalog_database(), PublishedSnapshots()
    with pytest.raises((ValueError, RuntimeError)):
        await catalog.sync_builtin_workflows(database=db, storage=storage, system_wiki=WIKI)
    assert not storage.revisions and not db.rows
    db.upsert_workflow_system.assert_not_awaited()


def example(key):
    root = REPOSITORY_ROOT / "workflow_packages" / key
    definition = json.loads((root / "workflow.json").read_text())["definition"]
    # Only these two reviewed examples execute in tests. Static package validation never imports.
    module = SimpleNamespace(**runpy.run_path(str(root / "main.py")))
    return module, definition


def test_deterministic_example_computes_and_rejects_bad_inputs():
    module, definition = example("example.csv_summary")
    result = module.run(None, {"csv_text": "id,amount_cents\na,1200\nb,2300\n"})
    assert "3500" in result["content"] and "Rows: 2" in result["content"]
    validate_code_result(json.dumps(result).encode(), validate_code_definition(definition))
    for text in ["wrong\n10\n", "amount_cents\n-1\n", "amount_cents\n", "amount_cents\n1,2\n"]:
        with pytest.raises(ValueError):
            module.run(None, {"csv_text": text})


@pytest.mark.parametrize("invalid_ids", [False, True])
async def test_two_model_steps_validate_then_pass_results_to_next_step(invalid_ids):
    module, definition = example("example.feedback_digest")
    spec = validate_code_definition(definition)
    calls = []

    async def generate(**payload):
        request_contract(spec, payload)
        calls.append(payload)
        if payload["step"] == "classify_feedback":
            output = {"items": [{"id": 99 if invalid_ids else 0, "category": "bug"}]}
        else:
            assert payload["data"] == [{"id": 0, "text": "The export failed", "category": "bug"}]
            output = {"points": ["The supplied feedback reports a failed export."]}
        jsonschema.validate(output, payload["output_schema"])
        return {"parsed": output, "text": json.dumps(output)}

    context = SimpleNamespace(models=SimpleNamespace(generate=generate))
    if invalid_ids:
        with pytest.raises(ValueError, match="preserve every input ID"):
            await module.run(context, {"feedback": ["The export failed"]})
        assert len(calls) == 1
    else:
        result = await module.run(context, {"feedback": ["The export failed"]})
        assert [c["step"] for c in calls] == ["classify_feedback", "summarize_feedback"]
        validate_code_result(json.dumps(result).encode(), spec)
        assert "bug: 1" in result["content"]


def test_estimate_covers_both_declared_model_steps():
    _, definition = example("example.feedback_digest")
    # One gpt-6-luna call's bound is about $0.003, so the cent-rounded ceilings of one and two
    # single-call steps are both $0.01. Four calls per step make each step's bound exceed a cent.
    for route in definition["code"]["model_routes"].values():
        route["max_calls"] = 4
    both = model_terms(definition)
    one = deepcopy(definition)
    del one["code"]["model_routes"]["summarize"]
    assert both["maximum_nanos"] > model_terms(one)["maximum_nanos"]


class PublicStorage(CheckpointStorage):
    def __init__(self):
        super().__init__()
        self.registry = PublishedSnapshots()

    async def publish_workflow_files(self, **kwargs):
        return await self.registry.publish_workflow_files(**kwargs)

    async def read_workflow_resource(self, *, repo_id, **kwargs):
        if repo_id == "registry/workflows":
            return await self.registry.read_workflow_resource(**kwargs)
        return await super().read_workflow_resource(repo_id=repo_id, **kwargs)


async def test_public_code_mcp_dashboard_discovery_execution_and_zero_credit_output(
    billed, monkeypatch
):
    f = billed
    (entry,) = select(monkeypatch, "example.csv_summary")
    storage = PublicStorage()
    compute = SyntheticCompute(
        {"path": "reports/CSV_SUMMARY.md", "content": "# CSV summary\n\nTotal: 3500\n"}
    )
    server, _, code = await setup(f, monkeypatch, compute=compute, storage=storage)
    f.settings.private_workflow_projects = set()
    await catalog.sync_builtin_workflows(database=f.db, storage=storage, system_wiki=WIKI)
    workflow = await f.db.get_workflow(entry.id)
    assert workflow.project_id is None
    require_private_execution(f.settings, workflow, f.project.id)
    with pytest.raises(PrivateWorkflowError):
        require_private_execution(f.settings, f.workflow, f.project.id)

    listed = structured(await server.call_tool("list_workflows", {"project_id": str(f.project.id)}))
    assert entry.key in str(listed)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="http://test"
    ) as client:
        response = await client.get("/api/workflows", params={"project_id": str(f.project.id)})
        assert response.status_code == 200 and entry.key in response.text
    run = await start(
        f, server, {"workflow_id": str(entry.id)}, inputs={"csv_text": "amount_cents\n3500\n"}
    )
    run_id = run["id"]
    env = ActivityEnvironment()
    await env.run(code.execute, run_id)
    await env.run(code.execute, run_id)
    await env.run(code.publish, run_id)
    await code.project(run_id)
    saved = await f.db.get_run(UUID(run_id))
    assert saved.definition_commit_sha == workflow.current_commit_sha
    assert saved.status == RunStatus.SUCCEEDED and compute.calls == 1
    output = await read_run_output(storage=storage, run=saved, repo_id=f.project.state_repo_id)
    assert b"3500" in output.content
    assert (await f.billing.run_charge(saved.id, ACTOR))["charged_usd"] == "0.00"
    assert await f.db.pool.fetchval("SELECT count(*) FROM billing_operations") == 0


async def test_public_multistep_model_recovery_reuses_paid_results(billed, monkeypatch):
    f = billed
    await fund(f)
    (entry,) = select(monkeypatch, "example.feedback_digest")
    module, _ = example(entry.key)

    class FeedbackCompute(SyntheticCompute):
        async def run_code_and_kill(self, *, packet, model_call, **_):
            self.calls += 1

            async def generate(**payload):
                result = await model_call(payload)
                if self.calls == 1:
                    raise RuntimeError("worker lost after the first model step")
                return result

            context = SimpleNamespace(models=SimpleNamespace(generate=generate))
            return json.dumps(await module.run(context, packet["inputs"])).encode()

    compute, storage = FeedbackCompute(), PublicStorage()
    server, common, _ = await setup(f, monkeypatch, compute=compute, storage=storage)
    f.settings.private_workflow_projects = set()
    await catalog.sync_builtin_workflows(database=f.db, storage=storage, system_wiki=WIKI)
    active = {"workflow_id": str(entry.id)}
    run_id = (await start(f, server, active, inputs={"feedback": ["The export failed"]}))["id"]
    first_router, first_calls = sdk_router(f, outputs=[{"items": [{"id": 0, "category": "bug"}]}])
    try:
        first = CodeActivities(common=common, model_router=first_router)
        with pytest.raises(RuntimeError, match="worker lost"):
            await ActivityEnvironment().run(first.execute, run_id)
        assert len(first_calls) == 1
    finally:
        await first_router.close()

    # A fresh router and activity instance have no in-memory model state to reuse.
    second_router, second_calls = sdk_router(f, outputs=[{"points": ["The export failed."]}])
    try:
        recovered = CodeActivities(common=common, model_router=second_router)
        await ActivityEnvironment().run(recovered.execute, run_id)
        await recovered.publish(run_id)
        assert len(second_calls) == 1 and compute.calls == 2
        assert await recovered.review(run_id)
        assert (await f.db.get_run(UUID(run_id))).status == RunStatus.NEEDS_INPUT
        with pytest.raises(RuntimeError, match="before review"):
            await recovered.project(run_id)
        await recovered.approve(run_id)
        await recovered.project(run_id)
        await f.billing.settle(UUID(run_id))
        await f.billing.settle(UUID(run_id))
        assert (await f.db.get_run(UUID(run_id))).status == RunStatus.SUCCEEDED
        assert await f.db.pool.fetchval("SELECT count(*) FROM billing_operations") == 2
        assert (
            await f.db.pool.fetchval(
                "SELECT count(*) FROM effect_receipts WHERE operation='code_model_call_v1'"
            )
            == 2
        )
        charge = await f.billing.run_charge(UUID(run_id), ACTOR)
        operations = await f.db.pool.fetch("SELECT status, observed_nanos FROM billing_operations")
        assert all(row["status"] == "observed" for row in operations)
        assert sum(row["observed_nanos"] for row in operations) == 1_500_000
        # Two small Luna calls are below one cent. Preserve supplier precision; the existing
        # policy rounds once at root settlement, not up to a cent for each model step.
        assert charge["status"] == "settled" and charge["charged_usd"] == "0.00"
        assert (
            await f.db.pool.fetchval(
                "SELECT count(*) FROM billing_ledger WHERE run_id=$1 AND kind='charge'",
                UUID(run_id),
            )
            == 1
        )
    finally:
        await second_router.close()


async def test_parent_cannot_publish_without_its_selected_package_child(monkeypatch):
    parent = next(w for w in catalog.BUILTIN_WORKFLOWS if w.key == "social.x_draft")
    select(monkeypatch)
    monkeypatch.setattr(catalog, "BUILTIN_WORKFLOWS", (*catalog.BUILTIN_WORKFLOWS, parent))
    db, storage = catalog_database(), PublishedSnapshots()
    with pytest.raises(RuntimeError, match="needs selected children.*social.x_compose"):
        await catalog.sync_builtin_workflows(database=db, storage=storage, system_wiki=WIKI)
    assert not storage.revisions and not db.rows
    db.upsert_workflow_system.assert_not_awaited()
