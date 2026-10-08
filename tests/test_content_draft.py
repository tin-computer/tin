from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from temporalio.exceptions import ApplicationError
from test_content_programs import setup
from test_private_workflows import ACTOR, app, mcp, structured
from test_procedure_publication import publication_db as publication_db

from tin_lite import content_draft
from tin_lite.activities import TinActivities
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.content_draft_sources import ContentDraftSources
from tin_lite.content_plan import plan_path
from tin_lite.organic_audit import canonical_json
from tin_lite.procedures import load_pinned_codex_procedure, validate_procedure_artifact
from tin_lite.publication import OutputCheckpoint
from tin_lite.run_service import start_workflow_run
from tin_lite.workflow_inputs import WorkflowInputError
from tin_lite.workflow_prerequisites import PrerequisiteError
from tin_lite.writing_style import STYLE_PATH


async def fixture(db, monkeypatch, *, editorial=False, clean=False, judgment=False, assets=False):
    db, storage, project, configured, planner, model, create = await setup(
        db, monkeypatch, editorial=editorial
    )
    initial = await create()
    await planner.execute(str(initial.id))
    await db.record_tin_user(ACTOR)
    await db.grant_project_membership(project_id=project.id, clerk_user_id=ACTOR)
    spec = next(w for w in BUILTIN_WORKFLOWS if w.key == content_draft.KEY)
    if not judgment:
        # Keep the historical single-file contract under regression coverage.
        spec = replace(
            spec,
            version_label="1.3.0" if clean else "1.2.0",
            procedure=replace(
                spec.procedure,
                output_validator=(
                    content_draft.CLEAN_VALIDATOR if clean else content_draft.VALIDATOR
                ),
                # Historical contracts had no assets folder.
                output_assets=None,
            ),
        )
    if assets:
        from tin_lite.page_assets import AssetPolicy

        policy = AssetPolicy(max_files=12, max_bytes=2_000_000)
        spec = replace(spec, procedure=replace(spec.procedure, output_assets=policy))
    definition, resources = spec.definition_and_resource_files()
    await db.upsert_registry_workflow(
        workflow_id=spec.id,
        key=spec.key,
        title=spec.title,
        description=spec.description,
        executor=spec.executor,
        definition_repo_id="registry/workflows",
        definition_path=spec.definition_path,
        current_commit_sha="e" * 40,
        version_label=spec.version_label,
        definition=definition,
    )
    read = storage.read_canonical_artifact

    async def read_resource(**kw):
        if kw["repo_id"] == "registry/workflows" and kw["commit_sha"] == "e" * 40:
            return (
                canonical_json(definition)
                if kw["path"] == spec.definition_path
                else resources[kw["path"]]
            )
        return await read(**kw)

    storage.read_canonical_artifact = read_resource
    storage.repo.edit(
        {STYLE_PATH: b"# Writing style\nExplicit preferences: Concrete mechanisms.\n"}
    )
    settings = SimpleNamespace(
        codex_api_projects={project.id},
        luna_api_key="synthetic",
        task_queue="test",
        switchboard_public_url="https://tin.test",
        clerk_frontend_api_url="https://clerk.test",
    )
    runtime = SimpleNamespace(
        database=db,
        storage=storage,
        temporal=SimpleNamespace(start_workflow=AsyncMock()),
        integrations=SimpleNamespace(ensure_requirements=AsyncMock()),
    )
    service = ContentDraftSources(database=db, storage=storage)
    choices = await service.discover(project_id=project.id, program_id=configured.id)
    return SimpleNamespace(
        db=db,
        storage=storage,
        project=project,
        configured=configured,
        planner=planner,
        model=model,
        initial=initial,
        service=service,
        runtime=runtime,
        settings=settings,
        workflow=await db.get_workflow(spec.id),
        inputs={
            "program_id": str(configured.id),
            "item_id": choices["items"][0]["id"],
            "plan_revision": choices["plan_revision"],
        },
    )


async def start(f, *, inputs=None, key=None):
    return await start_workflow_run(
        runtime=f.runtime,
        settings=f.settings,
        workflow=f.workflow,
        project_id=f.project.id,
        started_by_clerk_user_id=ACTOR,
        input_payload=inputs or f.inputs,
        start_idempotency_key=key,
    )


def article(context):
    header = "\n".join(
        f"{key}: {value}" for key, value in content_draft.provenance(context).items()
    )
    body = (
        "# Useful buyer task\n\n" + "A specific mechanism helps answer the buyer's question. " * 8
    )
    notes = "\n## Verification notes\n" + "\n".join(
        f"### v{i} — unresolved\n{requirement}. "
        "This requires a current primary source before publication.\n"
        for i, requirement in enumerate(context["item"]["verification"], 1)
    )
    return f"---\n{header}\n---\n{body}{notes}".encode()


async def test_source_discovery_http_mcp_and_authorization(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    server = mcp(f, monkeypatch)
    args = {"project_id": str(f.project.id), "program_id": str(f.configured.id)}
    result = structured(await server.call_tool("get_content_draft_sources", args))
    assert result["items"][0]["available"]
    assert result["items"][0]["readiness"] == "needs_verification"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        response = await client.get(
            f"/api/projects/{f.project.id}/content-drafts/sources",
            params={"program_id": str(f.configured.id)},
        )
        assert response.status_code == 200 and response.json() == result
    contract = structured(
        await server.call_tool(
            "get_workflow", {"project_id": str(f.project.id), "workflow_key": content_draft.KEY}
        )
    )
    assert contract["preparation"]["programs"][0]["id"] == str(f.configured.id)
    assert "read_project_file" in contract["preparation"]["instruction"]
    denied = mcp(f, monkeypatch, actor="user_outsider")
    with pytest.raises(ToolError, match="project not found"):
        await denied.call_tool("get_content_draft_sources", args)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f, "user_outsider")), base_url="https://tin.test"
    ) as client:
        assert (
            await client.get(f"/api/projects/{f.project.id}/content-drafts/sources")
        ).status_code == 404
    other = await f.db.create_project(name="Other", state_repo_id="other")
    with pytest.raises(LookupError):
        await f.service.select(project_id=other.id, inputs=f.inputs)
    f.runtime.temporal.start_workflow.assert_not_called()


async def test_stale_deferred_and_unknown_selection_before_compute(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    for inputs in ({**f.inputs, "item_id": "missing"}, {**f.inputs, "program_id": str(uuid4())}):
        with pytest.raises((WorkflowInputError, PrerequisiteError)):
            await start(f, inputs=inputs)
    plan = (await f.service.programs.read(project_id=f.project.id, program_id=f.configured.id))[
        "plan"
    ]
    original = deepcopy(plan)
    plan["batches"][0]["items"][0]["brief"] = "A changed brief"
    f.storage.repo.edit({plan_path(f.configured.id): canonical_json(plan)})
    with pytest.raises(WorkflowInputError, match="changed after selection"):
        await start(f)
    original["batches"][0]["items"][0]["readiness"] = "deferred"
    head = f.storage.repo.edit({plan_path(f.configured.id): canonical_json(original)})
    with pytest.raises(WorkflowInputError, match="deferred"):
        await start(f, inputs={**f.inputs, "plan_revision": head})
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM workflow_runs WHERE workflow_id=$1", f.workflow.id
        )
        == 0
    )


async def test_pinned_context_and_idempotent_start_survive_plan_style_changes(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch)
    key = str(uuid4())
    run = await start(f, key=key)
    prepared = await f.service.prepare(run)
    assert prepared["evidence"] and not prepared["unresolved_source_ids"]
    old_plan = f.storage.repo.trees[prepared["project_revision"]][plan_path(f.configured.id)][1]
    f.storage.repo.edit(
        {STYLE_PATH: b"New voice", plan_path(f.configured.id): b"Broken future edit"}
    )
    assert (await start(f, key=key)).id == run.id
    assert await f.service.prepare(run) == prepared
    assert prepared["item"]["readiness"] == "needs_verification" and f.model.calls == 1
    activities = TinActivities(
        database=f.db,
        storage=f.storage,
        settings=f.settings,
        sandboxes=SimpleNamespace(create=AsyncMock(return_value="sandbox-test")),
    )
    _, spec = await activities._pinned_codex_procedure(run.id)
    assert spec.output_path == f"content/drafts/{run.id}.md"
    assert spec.sandbox_context(inputs=run.input)["content_draft"] == prepared
    validate_procedure_artifact(article(prepared), spec=spec)
    await activities.create_codex_procedure_sandbox(str(run.id))
    attached = await f.db.get_run(run.id)
    assert attached.expected_head_sha == prepared["project_revision"]
    assert (
        f.storage.repo.trees[prepared["project_revision"]][plan_path(f.configured.id)][1]
        == old_plan
    )


async def test_draft_output_provenance_checks_and_unique_paths(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    run = await start(f)
    context = await f.service.prepare(run)
    spec = await load_pinned_codex_procedure(
        storage=f.storage,
        repo_id="registry/workflows",
        commit_sha="e" * 40,
        definition_path=f.workflow.definition_path,
    )
    first = spec.resolve_inputs(run.input, run_id=run.id)
    second = spec.resolve_inputs(run.input, run_id=uuid4())
    assert first.output_path != second.output_path and run.review_required
    first = replace(first, content_draft_context=context)
    good = article(context)
    validate_procedure_artifact(good, spec=first)
    for bad in (
        good.replace(context["item"]["id"].encode(), b"other"),
        good.replace(b"v1", b"v8"),
        good.replace(b"unresolved", b"approved"),
        b"# Unbound draft",
    ):
        with pytest.raises(ValueError):
            validate_procedure_artifact(bad, spec=first)
    with pytest.raises(ValueError):
        spec.resolve_inputs(run.input)


async def test_amendment_hold_is_unavailable_before_compute(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    await f.service.programs.begin_revision(
        project_id=f.project.id,
        program_id=f.configured.id,
        request_id=uuid4(),
        expected_revision=f.storage.repo.head,
        batch_ids=["week_01"],
        instruction="Revise the upcoming topic.",
        context_paths=[],
        actor=ACTOR,
    )
    choices = await f.service.discover(project_id=f.project.id, program_id=f.configured.id)
    assert not choices["items"][0]["available"]
    with pytest.raises(WorkflowInputError, match="pending revision"):
        await start(f)
    f.runtime.temporal.start_workflow.assert_not_called()


async def test_research_identity_and_style_bound_before_sandbox(publication_db, monkeypatch):
    f = await fixture(publication_db, monkeypatch)
    plan = (await f.service.programs.read(project_id=f.project.id, program_id=f.configured.id))[
        "plan"
    ]
    plan["host"] = "unrelated.example"
    head = f.storage.repo.edit({plan_path(f.configured.id): canonical_json(plan)})
    with pytest.raises(WorkflowInputError, match="site or market"):
        await start(f, inputs={**f.inputs, "plan_revision": head})
    plan["host"] = "example.com"
    head = f.storage.repo.edit(
        {plan_path(f.configured.id): canonical_json(plan), STYLE_PATH: b"x" * 24_001}
    )
    run = await start(f, inputs={**f.inputs, "plan_revision": head})
    activities = TinActivities(
        database=f.db,
        storage=f.storage,
        settings=f.settings,
        sandboxes=SimpleNamespace(create=AsyncMock()),
    )
    with pytest.raises(ValueError, match="24 KB"):
        await f.service.prepare(run)
    with pytest.raises(ValueError, match="Prepare"):
        await activities.create_codex_procedure_sandbox(str(run.id))
    activities._sandboxes.create.assert_not_called()


async def test_file_evidence_uses_pinned_checkout_and_gaps_stay_explicit(
    publication_db, monkeypatch
):
    f = await fixture(publication_db, monkeypatch)
    plan = (await f.service.programs.read(project_id=f.project.id, program_id=f.configured.id))[
        "plan"
    ]
    plan["batches"][0]["items"][0]["source_ids"] += [
        "file:notes/product.md",
        "file:notes/missing.md",
        "keyword:missing",
    ]
    head = f.storage.repo.edit(
        {
            plan_path(f.configured.id): canonical_json(plan),
            "notes/product.md": b"Known product facts",
        }
    )
    run = await start(f, inputs={**f.inputs, "plan_revision": head})
    context = await f.service.prepare(run)
    row = next(row for row in context["evidence"] if row["source_id"] == "file:notes/product.md")
    assert row["revision"] == head and row["content"] == "Known product facts"
    assert context["unresolved_source_ids"] == ["file:notes/missing.md", "keyword:missing"]
    f.storage.repo.edit({"notes/product.md": b"Changed facts"})
    assert await f.service.prepare(run) == context
    with pytest.raises(ValueError, match="too large"):
        content_draft.bounded({"evidence": "x" * content_draft.MAX_CONTEXT_BYTES})


@pytest.mark.parametrize("editorial", [False, True])
async def test_real_publication_retry_keeps_roadmap_and_requires_review(
    publication_db, monkeypatch, editorial
):
    f = await fixture(publication_db, monkeypatch, editorial=editorial)
    run = await start(f)
    context = await f.service.prepare(run)
    activities = TinActivities(
        database=f.db,
        storage=f.storage,
        settings=f.settings,
        sandboxes=SimpleNamespace(create=AsyncMock(return_value="sandbox-test"), kill=AsyncMock()),
    )
    await activities.create_codex_procedure_sandbox(str(run.id))
    run = await f.db.get_run(run.id)
    content = article(context)
    path = f"content/drafts/{run.id}.md"
    base = f.storage.repo.head
    revision = f.storage.repo.edit({path: content}, parent=run.expected_head_sha)
    f.storage.branch_revision = revision
    f.storage.repo.head = base
    checkpoint = OutputCheckpoint.create(
        run=run, revision=revision, path=path, media_type="text/markdown", content=content
    )
    key = f"{run.id}:procedure_artifact_persist"
    async with f.db.effect_lock(key, "procedure_artifact_persist") as (conn, _):
        await f.db.start_effect(conn, execution_key=key, operation="procedure_artifact_persist")
        await f.db.complete_procedure_persist(
            conn,
            execution_key=key,
            run_id=run.id,
            result={
                "checkpoint": checkpoint.to_dict(),
                "summary": "Draft ready",
                "sandbox_killed": True,
            },
            sandbox_killed=True,
        )
    # A concurrent future edit and a lost successful response use the existing CAS recovery.
    f.storage.repo.edit({STYLE_PATH: b"A later guide", "notes/later.md": b"Future planning note"})
    before = dict(f.storage.repo.trees[f.storage.repo.head])
    writes = f.storage.repo.writes
    f.storage.repo.lose_response = True
    with pytest.raises(ApplicationError, match="PublicationPendingError"):
        await activities.commit_codex_procedure_artifact(str(run.id))
    await activities.commit_codex_procedure_artifact(str(run.id))
    await activities.commit_codex_procedure_artifact(str(run.id))
    assert f.storage.repo.writes == writes + 1
    assert f.storage.repo.trees[f.storage.repo.head] == {**before, path: ("100644", content)}
    assert await activities.request_codex_procedure_review(str(run.id))
    assert (await f.db.get_run(run.id)).status.value == "needs_input"
    with pytest.raises(RuntimeError, match="review"):
        await activities.project_codex_procedure_result(str(run.id))
