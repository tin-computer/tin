"""First capture: bounded sources, exact preservation and the ordinary reviewed-pair path."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_private_workflows import ACTOR, app, fixture, mcp, structured
from test_procedure_publication import publication_db as publication_db

from tin_lite import brand_contract as brand
from tin_lite.activities import TinActivities
from tin_lite.brand_capture import BrandCaptureSources, resolve_brand, validate_pair
from tin_lite.procedures import load_pinned_codex_procedure
from tin_lite.public_workflows import load_public_workflows
from tin_lite.run_service import start_workflow_run
from tin_lite.workflow_inputs import WorkflowInputError

TOKENS = {
    "schema": brand.TOKEN_SCHEMA,
    "name": "Fixture",
    "light": {
        "ink": "#182B24",
        "paper": "#FAF8F0",
        "accent": "#287A55",
    },
}
ASSESSMENT = {
    "schema": brand.ASSESSMENT_SCHEMA,
    "identity_class": "distinctive_inconsistent",
    "evidence_status": "partial",
    "source_ids": ["site-home"],
    "captured_at": "2026-09-24T12:00:00Z",
    "findings": [
        {
            "observation": "The same palette has inconsistent heading hierarchy.",
            "source_ids": ["site-home"],
            "generation_implication": "Keep the palette and use one clear hierarchy.",
        }
    ],
    "method": {"kind": "visual_capture", "rubric_version": "marketing-brand.v1"},
}


def brand_doc(*, tokens=None, assessment=None):
    tokens = tokens or TOKENS
    assessment = assessment or ASSESSMENT
    palette = " ".join(c for key in ("light", "dark") for c in tokens.get(key, {}).values())
    return (
        "# Fixture identity\n\n## Brand direction\n"
        "Practical, direct guidance for product teams.\n\n"
        f"## Visual style\nObserved palette: {palette}. Use precise product illustrations.\n\n"
        "## Generation rules\nKeep the founder's green and use generous margins in new work.\n\n"
        "## Assessment and sources\nHomepage observed; deeper application screens unavailable.\n\n"
        f"```json\n{json.dumps(assessment)}\n```\n\n"
        '[site-home]: https://example.com/ "Homepage, 2026-09-24"\n\n'
        f"```json\n{json.dumps(tokens)}\n```\n"
    ).encode()


DESIGN = (
    "# Fixture design\n\n"
    + "\n\n".join(
        f"## {heading}\nObserved public site patterns; authenticated product states unavailable."
        for heading in brand.DESIGN_SECTIONS
    )
    + '\n\n[site-home]: https://example.com/ "Homepage, 2026-09-24"\n'
).encode()


async def setup(db):
    f = await fixture(db)
    package = next(p for p in await load_public_workflows() if p.key == brand.KEY)
    await db.upsert_registry_workflow(
        workflow_id=package.id,
        key=package.key,
        title=package.definition["title"],
        description=package.definition["description"],
        executor="codex.procedure",
        definition_repo_id="registry/workflows",
        definition_path=package.definition_path,
        current_commit_sha="d" * 40,
        version_label="1.0.0",
        definition=package.definition,
    )
    read = f.storage.read_canonical_artifact

    async def resources(**kw):
        return (
            package.files[kw["path"]] if kw["repo_id"] == "registry/workflows" else await read(**kw)
        )

    f.storage.read_canonical_artifact = resources
    f.storage.read_workflow_resource = resources
    f.workflow = await db.get_workflow(package.id)
    f.sources = BrandCaptureSources(
        database=db, storage=f.storage, integrations=f.runtime.integrations
    )
    f.procedure = await load_pinned_codex_procedure(
        storage=f.storage,
        repo_id="registry/workflows",
        commit_sha="checkout",
        definition_path=package.definition_path,
    )
    return f


async def start(f, **inputs):
    return await start_workflow_run(
        runtime=f.runtime,
        settings=f.settings,
        workflow=f.workflow,
        project_id=f.project.id,
        started_by_clerk_user_id=ACTOR,
        input_payload=inputs,
    )


@pytest.mark.parametrize("identity", sorted(brand.CLASSES))
def test_identity_classes_do_not_change_required_documents(identity):
    report = {**ASSESSMENT, "identity_class": identity}
    assert brand.validate_new_brand(brand_doc(assessment=report).decode()) == TOKENS
    brand.validate_new_design(DESIGN.decode())


@pytest.mark.parametrize(
    "change",
    [
        lambda t: t.update(unexpected="field"),
        lambda t: t["light"].update(accent="#fff"),
        lambda t: t.update(dark={"ink": "#000000"}),
        lambda t: t.update(type={"body": 12}),
        lambda t: t.update(shape="pill"),
    ],
)
def test_token_contract_rejects_invalid_data(change):
    value = deepcopy(TOKENS)
    change(value)
    with pytest.raises(ValueError):
        brand.tokens(brand_doc(tokens=value).decode())


def test_reject_duplicate_keys_blocks_and_unattributed_assessment():
    text = brand_doc().decode()
    for invalid in (
        text.replace(
            '"schema": "tin-brand.v1"', '"schema": "tin-brand.v1", "schema": "tin-brand.v1"'
        ),
        text + f"\n```json\n{json.dumps(TOKENS)}\n```\n",
        text.replace("[site-home]:", "[wrong-source]:"),
        text.replace("## Visual style", "## Vibe"),
        text.replace("Observed palette: #182B24", "Observed palette: unknown"),
    ):
        with pytest.raises(ValueError):
            brand.validate_new_brand(invalid)


def test_block_selection_uses_schema_field_not_incidental_mentions():
    report = deepcopy(ASSESSMENT)
    report["findings"][0]["observation"] = "The tin-brand.v1 palette matches the supplied page."
    assert brand.validate_new_brand(brand_doc(assessment=report).decode()) == TOKENS
    text = brand_doc().decode().replace('"identity_class":', '"identity_class" INVALID:')
    assert brand.tokens(text) == TOKENS
    with pytest.raises(ValueError, match="Invalid tin-brand-assessment.v1 JSON"):
        brand.assessment(text)


async def test_url_only_capture_prepares_before_compute_and_reuses_revision(publication_db):
    f = await setup(publication_db)
    run = await start(f, product_url="https://example.com/", notes="Keep our green")
    first = await f.sources.prepare(run, f.procedure)
    assert not any(v["present"] for v in first["documents"].values())
    assert first["repository"] is None and first["notes"] == "Keep our green"
    f.storage.repo.edit({brand.DESIGN_PATH: b"A founder's later design notes\n"})
    assert await f.sources.prepare(run, f.procedure) == first
    activities = TinActivities(
        database=f.db,
        storage=f.storage,
        settings=f.settings,
        sandboxes=SimpleNamespace(),
        integrations=f.runtime.integrations,
    )
    _, pinned = await activities._pinned_codex_procedure(run.id)
    context = pinned.sandbox_context(inputs=run.input)
    assert context["brand_capture"] == first
    assert "Keep our green" in context["prompt"]
    assert context["sandbox"]["profile"] == "browser"
    assert pinned.output_path.endswith(f"{run.id}/BRAND.md")
    assert pinned.companion_path.endswith(f"{run.id}/DESIGN.md")


async def test_missing_sources_and_existing_pair_fail_before_start(publication_db):
    f = await setup(publication_db)
    with pytest.raises(WorkflowInputError, match="Provide a product URL"):
        await start(f)
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 0
    f.runtime.temporal.start_workflow.assert_not_awaited()
    f.storage.repo.edit({brand.BRAND_PATH: brand_doc(), brand.DESIGN_PATH: DESIGN})
    with pytest.raises(WorkflowInputError, match="already exist"):
        await start(f, product_url="https://example.com/")
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 0


@pytest.mark.parametrize("raw", [b"x" * 64_001, b"\xff", b"# Credentials\n\x00"])
async def test_incompatible_existing_design_fails_before_start(publication_db, raw):
    f = await setup(publication_db)
    f.storage.repo.edit({brand.DESIGN_PATH: raw})
    with pytest.raises(WorkflowInputError, match="cannot be carried forward"):
        await start(f, product_url="https://example.com/")
    f.runtime.temporal.start_workflow.assert_not_awaited()


async def test_packet_only_and_attributed_memory_url(publication_db):
    f = await setup(publication_db)
    f.storage.repo.edit(
        {"inputs/brand.md": b"# Supplied evidence\nHomepage observations from the founder.\n"}
    )
    run = await start(f, source_path="inputs/brand.md", include_repository=False)
    result = await f.sources.prepare(run, f.procedure)
    assert result["source_packet"]["path"] == "inputs/brand.md" and not result["product_url"]
    f.storage.repo.edit({"wiki/INDEX.md": b"# Project\nWebsite: https://example.com/\n"})
    assert (await f.sources.inspect(f.project.id, {}))["product_url"] == "https://example.com/"
    with pytest.raises(ValueError, match="public HTTPS"):
        await f.sources.inspect(f.project.id, {"product_url": "https://user:pass@example.com/"})


async def test_repository_snapshot_is_selected_and_receipted_once(publication_db, monkeypatch):
    f = await setup(publication_db)
    connection = SimpleNamespace(id=uuid4(), configuration={"selected_repository": "owner/source"})
    monkeypatch.setattr(f.db, "get_integration_connection", AsyncMock(return_value=connection))
    bundle = SimpleNamespace(
        repository="owner/source", head_sha="e" * 40, file_count=12, complete=False
    )
    f.runtime.integrations.github_repository_bundle = AsyncMock(return_value=bundle)
    run = await start(f)
    context = await f.sources.prepare(run, f.procedure)
    assert context["source_snapshot"]["head_sha"] == "e" * 40
    assert context["source_snapshot"]["complete"] is False
    assert await f.sources.prepare(run, f.procedure) == context
    f.runtime.integrations.github_repository_bundle.assert_awaited_once()


async def test_pair_validator_preserves_nonstandard_member_documents(publication_db):
    f = await setup(publication_db)
    old = (
        b"# Member notes\nKeep annotations, including the blue site / green guide discrepancy.\n"
        b"[button]: ./components/button.tsx\n"
    )
    base = f.storage.repo.edit({brand.DESIGN_PATH: old})
    run = SimpleNamespace(expected_head_sha=base)
    await validate_pair(f.storage, f.project, run, brand_doc(), old)
    with pytest.raises(ValueError, match="unchanged"):
        await validate_pair(f.storage, f.project, run, brand_doc(), DESIGN)
    before = (
        b"# Our own format\nFounder direction stays.\n```json\n"
        + json.dumps(TOKENS).encode()
        + b"\n```\n"
    )
    base = f.storage.repo.edit({brand.BRAND_PATH: before, brand.DESIGN_PATH: None})
    await validate_pair(
        f.storage, f.project, SimpleNamespace(expected_head_sha=base), before, DESIGN
    )


async def test_active_resolver_never_reads_proposals_or_replaces_invalid_core(publication_db):
    f = await setup(publication_db)
    proposal = f.storage.repo.edit({"brand/proposals/run/BRAND.md": brand_doc()})
    assert (await resolve_brand(f.storage, f.project, proposal))["status"] == "missing"
    active = f.storage.repo.edit({brand.BRAND_PATH: brand_doc()})
    assert (await resolve_brand(f.storage, f.project, active))["tokens"] == TOKENS
    broken_optional = brand_doc().replace(
        b'"identity_class": "distinctive_inconsistent"', b'"identity_class": "not-a-class"'
    )
    edited = f.storage.repo.edit({brand.BRAND_PATH: broken_optional})
    result = await resolve_brand(f.storage, f.project, edited)
    assert (
        result["status"] == "available" and result["assessment"] is None and result["diagnostics"]
    )
    broken = f.storage.repo.edit({brand.BRAND_PATH: b"# No token block\n"})
    assert (await resolve_brand(f.storage, f.project, broken))["status"] == "brand_invalid"
    assert (await resolve_brand(f.storage, f.project, active))["tokens"] == TOKENS


async def test_http_and_mcp_reads_are_membership_bound_and_free(publication_db, monkeypatch):
    f = await setup(publication_db)
    f.storage.repo.edit({brand.BRAND_PATH: brand_doc()})
    server = mcp(f, monkeypatch)
    guide = structured(await server.call_tool("get_brand_guide", {"project_id": str(f.project.id)}))
    assert guide["current"]["documents"][brand.BRAND_PATH]["present"]
    result = structured(await server.call_tool("get_brand", {"project_id": str(f.project.id)}))
    assert result["tokens"] == TOKENS
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        response = await client.get(f"/api/projects/{f.project.id}/brand")
        assert response.status_code == 200 and response.json()["tokens"] == TOKENS
    f.storage.reads.clear()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f, actor="user_stranger")),
        base_url="https://tin.test",
    ) as client:
        assert (await client.get(f"/api/projects/{f.project.id}/brand")).status_code == 404
    assert (
        f.storage.reads == []
        and await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 0
    )


async def test_discovery_and_incomplete_start_offer_preparation(publication_db, monkeypatch):
    f = await setup(publication_db)
    server = mcp(f, monkeypatch)
    catalog = structured(
        await server.call_tool("list_workflows", {"project_id": str(f.project.id)})
    )
    rows = catalog["result"] if isinstance(catalog, dict) else catalog
    prep = next(row for row in rows if row["key"] == brand.KEY)["preparation"]
    assert prep["next_tool"]["name"] == "get_brand_guide" and "guide" not in prep
    detail = structured(
        await server.call_tool(
            "get_workflow",
            {
                "project_id": str(f.project.id),
                "workflow_key": brand.KEY,
            },
        )
    )
    assert detail["preparation"]["guide"]["outputs"] == [brand.BRAND_PATH, brand.DESIGN_PATH]
    with pytest.raises(ToolError) as caught:
        await server.call_tool(
            "start_workflow",
            {
                "project_id": str(f.project.id),
                "workflow_id": brand.KEY,
                "inputs": {},
            },
        )
    diagnostic = json.loads(str(caught.value).split(": ", 1)[1])
    assert diagnostic["preparation"] == prep
    assert "Provide a product URL" in diagnostic["error"]
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 0
    f.runtime.temporal.start_workflow.assert_not_awaited()


async def test_validated_capture_pair_is_reviewed_then_adopted(publication_db):
    from dataclasses import replace

    from tin_lite.publication import OutputCheckpoint
    from tin_lite.reviewed_documents import ReviewedDocuments

    f = await setup(publication_db)
    run = await start(f, product_url="https://example.com/")
    prepared = await f.sources.prepare(run, f.procedure)
    base = prepared["project_revision"]
    await f.db.pool.execute(
        "UPDATE workflow_runs SET expected_head_sha=$2, status='running' WHERE id=$1", run.id, base
    )
    run = replace(run, expected_head_sha=base)
    procedure = f.procedure.resolve_inputs(run.input, run_id=run.id)
    revision = f.storage.repo.edit(
        {procedure.output_path: brand_doc(), procedure.companion_path: DESIGN}
    )
    f.storage.repo.head = base
    activities = TinActivities(
        database=f.db,
        storage=f.storage,
        settings=f.settings,
        sandboxes=SimpleNamespace(),
        integrations=f.runtime.integrations,
    )
    companions = await activities._procedure_companions(run, f.project, procedure, revision)
    checkpoint = OutputCheckpoint.create(
        run=run,
        revision=revision,
        path=procedure.output_path,
        media_type="text/markdown",
        content=brand_doc(),
        companions=companions,
    )
    key = f"{run.id}:procedure_canonical_commit"
    sha, _ = await f.storage.publish_procedure_output(
        repo_id=f.project.state_repo_id,
        branch="main",
        checkpoint=checkpoint,
        content=brand_doc(),
        execution_key=key,
        workflow_key=brand.KEY,
        intent=None,
        legacy_attempt=False,
        save_intent=AsyncMock(),
        validate_lease=AsyncMock(),
    )
    async with f.db.effect_lock(key, "procedure_canonical_commit") as (conn, _):
        await f.db.start_effect(conn, execution_key=key, operation="procedure_canonical_commit")
        await f.db.complete_effect(
            conn,
            execution_key=key,
            result={
                "checkpoint": checkpoint.to_dict(),
                "artifact_path": procedure.output_path,
                "canonical_commit_sha": sha,
                "summary": "Brand and design ready to review.",
            },
        )
    await f.db.request_human_review(
        run_id=run.id,
        canonical_commit_sha=sha,
        artifact_path=procedure.output_path,
        artifact_ref=f"code.storage://{f.project.state_repo_id}@{sha}/{procedure.output_path}",
    )
    assert (await resolve_brand(f.storage, f.project, sha))["status"] == "missing"
    reviews = ReviewedDocuments(database=f.db, storage=f.storage)
    view = await reviews.view(run.id, ACTOR)
    assert view["palette_preview"] == TOKENS["light"]
    await reviews.approve(run_id=run.id, actor=ACTOR, token=view["review_token"])
    await activities.record_codex_procedure_approval(str(run.id))
    assert (await resolve_brand(f.storage, f.project, f.storage.repo.head))["tokens"] == TOKENS
    assert f.storage.repo.trees[f.storage.repo.head][brand.DESIGN_PATH][1] == DESIGN
    await activities.project_codex_procedure_result(str(run.id))
    assert (await f.db.get_run(run.id)).status.value == "succeeded"
