"""Approved text evidence is pinned before a code run and replayed from its receipt."""

import asyncio
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from temporalio.testing import ActivityEnvironment
from test_code_article_sources import fixture as article_fixture
from test_code_article_sources import start as start_article_consumer
from test_private_workflows import ACTOR
from test_procedure_publication import publication_db as publication_db
from test_workflow_code import CheckpointStorage, SyntheticCompute, definition

from tin_lite import approved_evidence, code_article_sources, code_evidence
from tin_lite.activities import TinActivities
from tin_lite.code_activities import CodeActivities
from tin_lite.private_workflows import (
    PackageActivation,
    PrivateWorkflows,
    validate_private_definition,
)
from tin_lite.publication import OutputCheckpoint
from tin_lite.run_service import start_workflow_run
from tin_lite.workflow_code import evidence_specs, validate_code_definition
from tin_lite.workflow_inputs import WorkflowInputError


def consumer(*, required=True):
    value = definition()
    value["key"] = "custom.evidence_consumer"
    value["code"]["files"] = ["main.py"]
    value["code"]["evidence"] = {
        "posts": {
            "kind": "approved_output",
            "input": "posts_run_id",
            "workflow_key": "social.article_fixture",
            "max_bytes": 16_000,
        }
    }
    value["input_schema"]["properties"]["posts_run_id"] = {
        "type": "string",
        "format": "uuid",
        "title": "Approved posts",
    }
    if required:
        value["input_schema"]["required"].append("posts_run_id")
    return value


def test_public_and_private_code_validate_bounded_optional_evidence():
    required = consumer()
    optional = consumer(required=False)
    assert validate_code_definition(required).evidence[0].required
    assert not validate_private_definition(optional).evidence[0].required
    assert evidence_specs(optional)[0].name == "posts"
    for change in (
        lambda d: d["code"]["evidence"]["posts"].update(kind="project_file"),
        lambda d: d["code"]["evidence"]["posts"].update(max_bytes=64_001),
        lambda d: d["code"]["evidence"]["posts"].update(input="project_id"),
        lambda d: d["input_schema"]["properties"]["posts_run_id"].update(format="uri"),
        lambda d: d.update(schedule_modes=["on_demand", "weekly"]),
    ):
        changed = deepcopy(required)
        change(changed)
        with pytest.raises(ValueError):
            validate_code_definition(changed)
    combined = deepcopy(required)
    combined["code"]["approved_article"] = {"input": "article_run"}
    combined["input_schema"]["properties"]["article_run"] = {
        "type": "string",
        "format": "uuid",
    }
    combined["input_schema"]["required"].append("article_run")
    combined["code"]["evidence"]["approved_article"] = combined["code"]["evidence"].pop("posts")
    with pytest.raises(ValueError, match="invalid code evidence slot"):
        validate_code_definition(combined)


async def fixture(db, monkeypatch, *, required=True, dynamic=False, private=False):
    f = await article_fixture(db, monkeypatch)
    if dynamic:
        f.definition["code"]["output"]["path"] = "reports/social/{date}-{slug}.md"
        revision = f.storage.repo.edit(
            {
                f.path: json.dumps(
                    {"package_format": "tin-workflow-package-v1", "definition": f.definition}
                ).encode()
            }
        )
        await f.db.upsert_registry_workflow(
            workflow_id=f.consumer.id,
            key=f.consumer.key,
            title=f.consumer.title,
            description=f.consumer.description,
            executor="workflow.code",
            definition_repo_id=f.project.state_repo_id,
            definition_path=f.path,
            current_commit_sha=revision,
            version_label="1.0.1",
            definition=f.definition,
        )
        f.consumer = await f.db.get_workflow(f.consumer.id)
    source = await start_article_consumer(f)
    output_path = (
        f.definition["code"]["output"]["path"]
        .replace("{date}", source.created_at.date().isoformat())
        .replace("{slug}", "release-one")
    )
    output = {"path": output_path, "content": "# Approved posts\n"}
    compute = SyntheticCompute(output)
    f.storage.branch_revision = None
    monkeypatch.setattr(
        f.storage, "stage_native_output", CheckpointStorage.stage_native_output.__get__(f.storage)
    )
    common = TinActivities(database=f.db, storage=f.storage, settings=f.settings, sandboxes=compute)
    code = CodeActivities(common=common)
    env = ActivityEnvironment()
    await env.run(code.execute, str(source.id))
    await code.publish(str(source.id))
    assert await code.review(str(source.id))
    await f.db.set_review_actor(run_id=source.id, clerk_user_id=ACTOR)
    await code.approve(str(source.id))
    await code.project(str(source.id))
    f.posts = await f.db.get_run(source.id)

    value = consumer(required=required)
    path = f"workflow_packages/{value['key']}/workflow.json"
    revision = f.storage.repo.edit(
        {
            path: json.dumps(
                {"package_format": "tin-workflow-package-v1", "definition": value}
            ).encode(),
            f"workflow_packages/{value['key']}/main.py": b"def run(ctx, inputs): pass\n",
        }
    )
    if private:
        f.settings.private_workflow_projects = {f.project.id}
        f.settings.e2b_isolated_template = "isolated-test"
        service = PrivateWorkflows(database=f.db, storage=f.storage, settings=f.settings)
        activated = await service.activate(
            project_id=f.project.id,
            actor=ACTOR,
            client_id=None,
            selection=PackageActivation(
                path=path, revision=revision, request_id=uuid4(), expected_revision=None
            ),
        )
        workflow_id = UUID(activated["workflow_id"])
    else:
        workflow_id = uuid4()
        await f.db.upsert_registry_workflow(
            workflow_id=workflow_id,
            key=value["key"],
            title="Evidence consumer",
            description="Read approved posts",
            executor="workflow.code",
            definition_repo_id=f.project.state_repo_id,
            definition_path=path,
            current_commit_sha=revision,
            version_label="1.0.0",
            definition=value,
        )
    f.evidence_workflow = await f.db.get_workflow(workflow_id)
    f.evidence_definition = value
    f.evidence_inputs = {"posts_run_id": str(source.id), "minimum_cents": 1000}
    return f


async def start(f, *, key=None, inputs=None):
    return await start_workflow_run(
        runtime=f.runtime,
        settings=f.settings,
        workflow=f.evidence_workflow,
        project_id=f.project.id,
        started_by_clerk_user_id=ACTOR,
        start_idempotency_key=key,
        input_payload=inputs or f.evidence_inputs,
    )


async def test_approved_output_admission_and_replay(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch, dynamic=True)
    request_id = str(uuid4())
    first, repeated = await asyncio.gather(start(f, key=request_id), start(f, key=request_id))
    assert first.id == repeated.id
    source = await code_evidence.saved_source(
        f.db, first, validate_code_definition(f.evidence_definition)
    )
    assert source["posts"]["content"] == "# Approved posts\n"
    assert source["posts"]["revision"] == f.posts.canonical_commit_sha
    assert "publication_checkpoint" not in source["posts"]
    f.storage.repo.edit({f.posts.artifact_path: b"# Later unapproved edit\n"})
    assert (await start(f, key=request_id)).id == first.id
    assert (
        await code_evidence.saved_source(
            f.db, first, validate_code_definition(f.evidence_definition)
        )
        == source
    )


async def test_optional_absence_is_pinned_and_selected_invalid_is_not_dropped(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch, required=False)
    absent = await start(f, inputs={"minimum_cents": 1000})
    assert await code_evidence.saved_source(
        f.db, absent, validate_code_definition(f.evidence_definition)
    ) == {"posts": {"present": False}}
    with pytest.raises(WorkflowInputError, match="Choose an approved"):
        await start(f, inputs={"posts_run_id": str(f.source.id), "minimum_cents": 1000})


async def test_activated_private_package_uses_same_approved_source(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch, private=True)
    assert f.evidence_workflow.project_id == f.project.id
    started = await start(f)
    source = await code_evidence.saved_source(
        f.db, started, validate_code_definition(f.evidence_definition)
    )
    assert source["posts"]["content"] == "# Approved posts\n"
    f.storage.repo.edit({f.posts.artifact_path: b"# A different current file\n"})
    f.storage.branch_revision = None

    class Compute(SyntheticCompute):
        async def run_code_and_kill(self, *, packet, **kwargs):
            self.evidence = deepcopy(packet["context"]["evidence"])
            return await super().run_code_and_kill(packet=packet, **kwargs)

    compute = Compute(
        {
            "path": f.evidence_definition["code"]["output"]["path"],
            "content": "# Provenance-preserving summary\n",
        }
    )
    common = TinActivities(database=f.db, storage=f.storage, settings=f.settings, sandboxes=compute)
    await ActivityEnvironment().run(CodeActivities(common=common).execute, str(started.id))
    assert compute.evidence["posts"]["content"] == "# Approved posts\n"
    assert "publication_checkpoint" not in compute.evidence["posts"]


async def test_guard_rejects_stale_approval_and_wrong_project(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    snapshot = await code_evidence.select(
        database=f.db,
        storage=f.storage,
        project_id=f.project.id,
        definition=f.evidence_definition,
        inputs=f.evidence_inputs,
    )
    args = dict(
        project_id=f.project.id,
        workflow_id=f.evidence_workflow.id,
        started_by_clerk_user_id=ACTOR,
        input_payload=f.evidence_inputs,
        pinned_definition=f.evidence_definition,
        definition_commit_sha=f.evidence_workflow.current_commit_sha,
    )
    with pytest.raises(ValueError, match="Select approved evidence"):
        await f.db.create_run(**args)
    await f.db.pool.execute(
        "UPDATE workflow_runs SET review_version=review_version+1 WHERE id=$1", f.posts.id
    )
    with pytest.raises(ValueError, match="approval or revision"):
        await f.db.create_run(**args, approved_evidence_source=snapshot)
    await f.db.pool.execute(
        "UPDATE workflow_runs SET review_version=review_version-1 WHERE id=$1", f.posts.id
    )
    other = await f.db.create_project(name="Other", state_repo_id="projects/other-evidence")
    with pytest.raises(WorkflowInputError, match="from this project"):
        await start(f, inputs={**f.evidence_inputs, "posts_run_id": str(other.id)})
    with pytest.raises(ValueError, match="from this project"):
        await approved_evidence.select_one(
            database=f.db,
            storage=f.storage,
            project_id=other.id,
            source_run_id=f.posts.id,
            workflow_key=f.consumer.key,
            max_bytes=16_000,
        )


async def test_publication_tamper_is_not_admitted(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    snapshot = await code_evidence.select(
        database=f.db,
        storage=f.storage,
        project_id=f.project.id,
        definition=f.evidence_definition,
        inputs=f.evidence_inputs,
    )
    receipt_key = f"{f.posts.id}:procedure_canonical_commit"
    publication = deepcopy((await f.db.get_effect(receipt_key)).result)
    publication["checkpoint"]["sha256"] = "0" * 64
    await f.db.pool.execute(
        "UPDATE effect_receipts SET result=$2::jsonb WHERE execution_key=$1",
        receipt_key,
        json.dumps(publication),
    )
    with pytest.raises(WorkflowInputError, match="checkpoint"):
        await start(f)
    with pytest.raises(ValueError, match="publication proof"):
        await f.db.create_run(
            project_id=f.project.id,
            workflow_id=f.evidence_workflow.id,
            started_by_clerk_user_id=ACTOR,
            input_payload=f.evidence_inputs,
            pinned_definition=f.evidence_definition,
            definition_commit_sha=f.evidence_workflow.current_commit_sha,
            approved_evidence_source=snapshot,
        )


async def test_selected_source_limit_rejects_oversized_text_before_admission(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch)
    with pytest.raises(ValueError, match="size limit"):
        await approved_evidence.select_one(
            database=f.db,
            storage=f.storage,
            project_id=f.project.id,
            source_run_id=f.posts.id,
            workflow_key=f.consumer.key,
            max_bytes=4,
        )


async def test_combined_article_and_evidence_bound_rejects_before_run_or_budget(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch)
    combined = deepcopy(f.evidence_definition)
    combined["code"]["approved_article"] = {"input": "article_run"}
    combined["input_schema"]["properties"]["article_run"] = {
        "type": "string",
        "format": "uuid",
    }
    combined["input_schema"]["required"].append("article_run")
    validate_code_definition(combined)
    inputs = {**f.evidence_inputs, "article_run": str(f.source.id)}
    article = await code_article_sources.select(
        database=f.db,
        storage=f.storage,
        project_id=f.project.id,
        definition=combined,
        inputs=inputs,
    )
    evidence = await code_evidence.select(
        database=f.db,
        storage=f.storage,
        project_id=f.project.id,
        definition=combined,
        inputs=inputs,
    )
    # Admission must enforce the shared envelope even if two individually valid
    # snapshots are supplied after an internal caller changes one snapshot.
    article["article"] = "A" * approved_evidence.MAX_TOTAL_BYTES
    article["article_sha256"] = hashlib.sha256(article["article"].encode()).hexdigest()
    before_budget = await f.db.pool.fetchval(
        "SELECT count(*) FROM billing_run_budgets WHERE project_id=$1", f.project.id
    )
    with pytest.raises(ValueError, match="Combined approved source context"):
        await f.db.create_run(
            project_id=f.project.id,
            workflow_id=f.evidence_workflow.id,
            started_by_clerk_user_id=ACTOR,
            input_payload=inputs,
            pinned_definition=combined,
            definition_commit_sha=f.evidence_workflow.current_commit_sha,
            approved_article_source=article,
            approved_evidence_source=evidence,
        )
    assert not await f.db.pool.fetchval(
        "SELECT id FROM workflow_runs WHERE workflow_id=$1", f.evidence_workflow.id
    )
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM billing_run_budgets WHERE project_id=$1", f.project.id
        )
        == before_budget
    )


async def test_supported_procedure_primary_text_uses_its_pinned_contract(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch)
    root = Path(__file__).parents[1] / "workflow_packages/example.posthog_funnel"
    manifest = json.loads((root / "workflow.json").read_text())
    definition = manifest["definition"]
    definition["human_review"] = {"eligible": True, "summary": "Review funnel"}
    files = {
        str(path.relative_to(root.parent)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    files["example.posthog_funnel/workflow.json"] = json.dumps(manifest).encode()
    files = {f"workflow_packages/{name}": raw for name, raw in files.items()}
    revision = f.storage.repo.edit(files)
    workflow_id = uuid4()
    await f.db.upsert_registry_workflow(
        workflow_id=workflow_id,
        key="example.posthog_funnel",
        title="Funnel",
        description="A reviewed synthetic source",
        executor="codex.procedure",
        definition_repo_id=f.project.state_repo_id,
        definition_path="workflow_packages/example.posthog_funnel/workflow.json",
        current_commit_sha=revision,
        version_label="2.0.0",
        definition=definition,
    )
    base = f.storage.repo.head
    source, created = await f.db.create_run(
        project_id=f.project.id,
        workflow_id=workflow_id,
        started_by_clerk_user_id=ACTOR,
        input_payload={
            "question": "Which onboarding path?",
            "start_date": "2026-01-01",
            "end_date": "2026-01-08",
        },
        pinned_definition=definition,
        definition_commit_sha=revision,
    )
    assert created
    path = "reports/POSTHOG_FUNNEL.md"
    raw = b"# Reviewed funnel\n"
    published_revision = f.storage.repo.edit({path: raw})
    await f.db.pool.execute(
        "UPDATE workflow_runs SET status='succeeded', review_decision='approved', "
        "reviewed_at=now(), expected_head_sha=$2, canonical_commit_sha=$3, "
        "artifact_path=$4 WHERE id=$1",
        source.id,
        base,
        published_revision,
        path,
    )
    source = await f.db.get_run(source.id)
    checkpoint = OutputCheckpoint.create(
        run=source,
        revision=published_revision,
        path=path,
        media_type="text/markdown",
        content=raw,
    )
    key = f"{source.id}:procedure_canonical_commit"
    async with f.db.effect_lock(key, "procedure_canonical_commit") as (conn, _):
        await f.db.start_effect(conn, execution_key=key, operation="procedure_canonical_commit")
        await f.db.complete_effect(
            conn,
            execution_key=key,
            result={
                "canonical_commit_sha": published_revision,
                "artifact_path": path,
                "checkpoint": checkpoint.to_dict(),
            },
        )
    selected = await approved_evidence.select_one(
        database=f.db,
        storage=f.storage,
        project_id=f.project.id,
        source_run_id=source.id,
        workflow_key="example.posthog_funnel",
        max_bytes=32_000,
    )
    assert selected["content"] == raw.decode()
    assert selected["definition_commit_sha"] == revision
