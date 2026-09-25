"""Exact-pair review over the existing publication/review receipts."""

import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from test_procedure_publication import (
    HistoryStorage,
    activity_fixture,
    run_fixture,
)
from test_procedure_publication import (
    publication_db as publication_db,
)

from tin_lite.api import router
from tin_lite.auth import AuthContext, require_user
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.procedure_repository import select_repository
from tin_lite.procedures import validate_codex_procedure_definition
from tin_lite.publication import OutputCheckpoint, OutputConflictError, PublicationPendingError
from tin_lite.reviewed_documents import ReviewedDocuments
from tin_lite.workflow_review_store import ReviewConflict

ACTOR = "user_documentreviewer"
DESTINATIONS = ("brand/BRAND.md", "DESIGN.md")
BRAND, DESIGN = b"# Brand\nA considered identity.\n", b"# Design\nObserved components.\n"


def definition():
    source = next(w for w in BUILTIN_WORKFLOWS if w.key == "content.public_article")
    result = deepcopy(source.definition_and_resource_files()[0])
    result["procedure"]["output"] = {
        "kind": "project.artifact",
        "path_template": "brand/proposals/{run_id}/BRAND.md",
        "media_type": "text/markdown",
        "max_bytes": 48_000,
        "companion": {
            "path_template": "brand/proposals/{run_id}/DESIGN.md",
            "max_bytes": 64_000,
            "label": "Design",
        },
        "apply_on_approval": {"primary": DESTINATIONS[0], "companion": DESTINATIONS[1]},
    }
    return result


def checkpoint_pair(storage, run):
    primary = f"brand/proposals/{run.id}/BRAND.md"
    companion = f"brand/proposals/{run.id}/DESIGN.md"
    revision = storage.repo.edit({primary: BRAND, companion: DESIGN})
    child = OutputCheckpoint.create(
        run=run, revision=revision, path=companion, media_type="text/markdown", content=DESIGN
    )
    return OutputCheckpoint.create(
        run=run,
        revision=revision,
        path=primary,
        media_type="text/markdown",
        content=BRAND,
        companions=(child,),
    )


async def setup(db, *, existing=None):
    activities, storage, run, _ = await activity_fixture(db, review=True)
    if existing:
        base = storage.repo.edit(existing)
        run = replace(run, expected_head_sha=base)
        await db.pool.execute(
            "UPDATE workflow_runs SET expected_head_sha=$2 WHERE id=$1", run.id, base
        )
    await db.pool.execute(
        "UPDATE workflows SET definition=$2::jsonb WHERE id=$1",
        run.workflow_id,
        json.dumps(definition()),
    )
    await db.record_tin_user(ACTOR)
    await db.grant_project_membership(project_id=run.project_id, clerk_user_id=ACTOR)
    checkpoint = checkpoint_pair(storage, run)
    revision = checkpoint.ephemeral_commit_sha
    key = f"{run.id}:procedure_canonical_commit"
    async with db.effect_lock(key, "procedure_canonical_commit") as (conn, _):
        await db.start_effect(conn, execution_key=key, operation="procedure_canonical_commit")
        await db.complete_effect(
            conn,
            execution_key=key,
            result={
                "checkpoint": checkpoint.to_dict(),
                "artifact_path": checkpoint.artifact_path,
                "canonical_commit_sha": revision,
            },
        )
    await db.request_human_review(
        run_id=run.id,
        canonical_commit_sha=revision,
        artifact_path=checkpoint.artifact_path,
        artifact_ref=f"code.storage://{storage.repo.id}@{revision}/{checkpoint.artifact_path}",
    )
    return SimpleNamespace(
        db=db,
        storage=storage,
        run=run,
        checkpoint=checkpoint,
        reviews=ReviewedDocuments(database=db, storage=storage),
        activities=activities,
    )


def test_pair_contract_is_bounded_and_requires_review():
    valid = definition()
    spec = validate_codex_procedure_definition(valid)
    assert spec.output_max_files == 2 and spec.documents.companion_max_bytes == 64_000
    for change in (
        lambda d: d.pop("human_review"),
        lambda d: d["procedure"]["output"]["companion"].update(max_bytes=64_001),
        lambda d: d["procedure"]["output"]["companion"].update(path_template="DESIGN.md"),
        lambda d: d["procedure"]["output"]["apply_on_approval"].update(primary="wiki/INDEX.md"),
        lambda d: d["procedure"]["output"]["apply_on_approval"].update(companion="brand/BRAND.md"),
        lambda d: d["procedure"]["output"]["apply_on_approval"].update(companion="../DESIGN.md"),
    ):
        invalid = deepcopy(valid)
        change(invalid)
        with pytest.raises(ValueError):
            validate_codex_procedure_definition(invalid)


async def approve(f):
    view = await f.reviews.view(f.run.id, ACTOR)
    await f.reviews.approve(run_id=f.run.id, actor=ACTOR, token=view["review_token"])
    return view


async def test_approval_applies_both_once_and_keeps_proposals(publication_db):
    f = await setup(publication_db)
    view = await f.reviews.view(f.run.id, ACTOR)
    assert [d["change"] for d in view["documents"]] == ["new", "new"]
    with pytest.raises(ReviewConflict, match="accepted exact-pair"):
        await f.reviews.apply(f.run.id)
    for token in (None, "0" * 64):
        with pytest.raises(ReviewConflict, match="Read both"):
            await f.reviews.approve(run_id=f.run.id, actor=ACTOR, token=token)
    await approve(f)
    # Ordinary edits can happen after review without invalidating either destination.
    f.storage.repo.edit({"notes/other.md": b"Another member's work\n"})
    assert all(p not in f.storage.repo.trees[f.storage.repo.head] for p in DESTINATIONS)
    await f.activities.record_codex_procedure_approval(str(f.run.id))
    head = f.storage.repo.head
    assert [f.storage.repo.trees[head][p][1] for p in DESTINATIONS] == [BRAND, DESIGN]
    assert f.storage.repo.writes == 1
    assert f.checkpoint.artifact_path in f.storage.repo.trees[head]
    f.storage.repo.edit({"DESIGN.md": b"Founder revision after adoption\n"})
    await f.activities.record_codex_procedure_approval(str(f.run.id))
    assert f.storage.repo.writes == 1
    assert f.storage.repo.trees[f.storage.repo.head]["DESIGN.md"][1].startswith(b"Founder")


@pytest.mark.parametrize("destination", DESTINATIONS)
@pytest.mark.parametrize("after_approval", [False, True])
async def test_either_destination_edit_prevents_both_writes(
    publication_db, destination, after_approval
):
    f = await setup(publication_db)
    if after_approval:
        await approve(f)
    f.storage.repo.edit({destination: b"Founder decision\n"})
    with pytest.raises((ReviewConflict, OutputConflictError)):
        if after_approval:
            await f.reviews.apply(f.run.id)
        else:
            await approve(f)
    assert f.storage.repo.writes == 0
    assert f.storage.repo.trees[f.storage.repo.head][destination][1] == b"Founder decision\n"


async def test_edited_companion_invalidates_review_and_unchanged_is_explicit(publication_db):
    f = await setup(publication_db, existing={"DESIGN.md": DESIGN})
    view = await f.reviews.view(f.run.id, ACTOR)
    assert [d["change"] for d in view["documents"]] == ["new", "unchanged"]
    f.storage.repo.edit({f.checkpoint.companions[0].artifact_path: b"Edited proposal\n"})
    with pytest.raises(ReviewConflict, match="edited"):
        await f.reviews.approve(run_id=f.run.id, actor=ACTOR, token=view["review_token"])
    assert not (await f.reviews.view(f.run.id, ACTOR))["can_approve"]
    assert f.storage.repo.writes == 0


async def test_apply_recovers_a_lost_ack_without_overwriting_later_edits(
    publication_db, monkeypatch
):
    f = await setup(publication_db)
    await approve(f)
    original = f.db.complete_effect

    async def fail_apply(conn, *, execution_key, result):
        if execution_key.endswith(":procedure_document_apply"):
            raise RuntimeError("Database unavailable after commit")
        return await original(conn, execution_key=execution_key, result=result)

    monkeypatch.setattr(f.db, "complete_effect", fail_apply)
    with pytest.raises(RuntimeError, match="Database unavailable"):
        await f.reviews.apply(f.run.id)
    assert f.storage.repo.writes == 1
    f.storage.repo.edit({"DESIGN.md": b"Later founder choice\n"})
    monkeypatch.setattr(f.db, "complete_effect", original)
    await f.reviews.apply(f.run.id)
    assert f.storage.repo.writes == 1
    assert f.storage.repo.trees[f.storage.repo.head]["DESIGN.md"][1] == b"Later founder choice\n"


@pytest.mark.parametrize("revoked", [False, True])
async def test_stopped_or_revoked_approval_cannot_apply(publication_db, revoked):
    f = await setup(publication_db)
    await approve(f)
    if revoked:
        await f.db.pool.execute(
            "DELETE FROM project_memberships WHERE project_id=$1", f.run.project_id
        )
    else:
        await f.db.pool.execute("UPDATE workflow_runs SET status='stopped' WHERE id=$1", f.run.id)
    with pytest.raises(ReviewConflict, match="authorized"):
        await f.reviews.apply(f.run.id)
    assert f.storage.repo.writes == 0


async def test_atomic_storage_retry_after_lost_response():
    storage, state = HistoryStorage(), {}
    run = run_fixture()
    checkpoint = checkpoint_pair(storage, run)
    storage.repo.lose_response = True

    async def save(value):
        state["intent"] = value

    kwargs = dict(
        repo_id=storage.repo.id,
        branch="main",
        checkpoint=checkpoint,
        proposal_revision=checkpoint.ephemeral_commit_sha,
        destinations=DESTINATIONS,
        execution_key=f"{run.id}:apply",
        save_intent=save,
        validate_authority=AsyncMock(),
    )
    with pytest.raises(PublicationPendingError):
        await storage.apply_reviewed_documents(**kwargs, intent=None)
    assert storage.repo.writes == 1
    storage.repo.edit({"brand/BRAND.md": b"Later edit\n"})
    await storage.apply_reviewed_documents(**kwargs, intent=state["intent"])
    assert storage.repo.writes == 1


@pytest.mark.parametrize("decision", [False, True])
async def test_http_approval_requires_pair_token(publication_db, decision):
    f = await setup(publication_db)
    app = FastAPI()
    app.include_router(router)
    app.state.runtime = SimpleNamespace(database=f.db, storage=f.storage)
    app.state.settings = SimpleNamespace()
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id=ACTOR,
        token_type="session",  # noqa: S106 - authentication kind
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://tin.test"
    ) as c:
        path = f"/api/workflows/runs/{f.run.id}"
        assert (await c.post(path + "/approve")).status_code == 409
        view = (await c.get(path + "/review")).json()
        endpoint = path + "/approve"
        if decision:
            decision_id = await f.db.pool.fetchval(
                "SELECT id FROM run_decisions WHERE run_id=$1", f.run.id
            )
            assert decision_id is not None
            endpoint = f"/api/decisions/{decision_id}/apply"
        response = await c.post(
            endpoint,
            json={
                "review_token": view["review_token"],
                **({"action": "approve"} if decision else {}),
            },
        )
        assert response.status_code == 202, response.text
        assert f.storage.repo.writes == 0  # Durable command dispatch performs the apply.


async def test_optional_repository_selection_is_pinned(publication_db, monkeypatch):
    f = await setup(publication_db)
    spec = SimpleNamespace(optional_repository=True, repository_input="include_repository")
    connection = SimpleNamespace(id=uuid4(), configuration={"selected_repository": "owner/source"})
    read = AsyncMock(return_value=None)
    monkeypatch.setattr(f.db, "get_integration_connection", read)
    assert not await select_repository(f.db, f.run, spec)
    read.return_value = connection
    assert not await select_repository(f.db, f.run, spec)  # No newly attached source mid-run.
    another = replace(f.run, id=uuid4())
    assert await select_repository(f.db, another, spec)
    read.return_value = SimpleNamespace(
        id=connection.id, configuration={"selected_repository": "other/repo"}
    )
    with pytest.raises(ValueError, match="changed"):
        await select_repository(f.db, another, spec)


def test_optional_source_contract_requires_matching_read_only_integration():
    value = definition()
    value["procedure"]["workspace"] = {
        "kind": "github.repository",
        "provider_key": "infra.github",
        "capabilities": ["contents.read"],
        "optional": True,
        "enabled_input": "include_repository",
    }
    value["input_schema"]["properties"]["include_repository"] = {
        "type": "boolean",
        "default": True,
    }
    value["integration_requirements"] = [
        {"provider_key": "infra.github", "capabilities": ["contents.read"], "required": False}
    ]
    assert validate_codex_procedure_definition(value).optional_repository
    for change in (
        lambda d: d["integration_requirements"][0].update(required=True),
        lambda d: d["procedure"]["workspace"].update(
            capabilities=["contents.read", "pull_requests.read"]
        ),
        lambda d: d["procedure"]["workspace"].update(enabled_input="undeclared"),
    ):
        invalid = deepcopy(value)
        change(invalid)
        with pytest.raises(ValueError):
            validate_codex_procedure_definition(invalid)


async def test_bounded_pair_loader_resolves_both_paths_and_context():
    from tin_lite.procedures import load_pinned_codex_procedure, validate_procedure_artifact

    source = next(w for w in BUILTIN_WORKFLOWS if w.key == "content.public_article")
    _, resources = source.definition_and_resource_files()
    value = definition()

    async def read(**kw):
        return (
            json.dumps(value).encode()
            if kw["path"] == source.definition_path
            else resources[kw["path"]]
        )

    procedure = await load_pinned_codex_procedure(
        storage=SimpleNamespace(read_canonical_artifact=read),
        repo_id="registry/workflows",
        commit_sha="d" * 40,
        definition_path=source.definition_path,
    )
    run_id = uuid4()
    procedure = procedure.resolve_inputs({}, run_id=run_id)
    output = procedure.sandbox_context(inputs={})["output"]
    assert output["path"] == f"brand/proposals/{run_id}/BRAND.md"
    assert output["companion_path"] == f"brand/proposals/{run_id}/DESIGN.md"
    assert output["reviewed_documents"] and output["companion_max_bytes"] == 64_000
    for content in (b"", b"\xff", b"# Brand\x00", b"x" * 48_001):
        with pytest.raises(ValueError):
            validate_procedure_artifact(content, spec=procedure)


async def test_mcp_pair_review_and_token_approval(publication_db, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError
    from test_private_workflows import mcp, structured

    f = await setup(publication_db)
    f.settings = SimpleNamespace(
        switchboard_public_url="https://tin.test", clerk_frontend_api_url="https://clerk.test"
    )
    f.runtime = SimpleNamespace(database=f.db, storage=f.storage)
    server = mcp(f, monkeypatch, actor=ACTOR)
    with pytest.raises(ToolError, match="Read both"):
        await server.call_tool("approve_workflow_run", {"run_id": str(f.run.id)})
    view = structured(await server.call_tool("get_workflow_review", {"run_id": str(f.run.id)}))
    assert len(view["documents"]) == 2
    result = structured(
        await server.call_tool(
            "approve_workflow_run",
            {
                "run_id": str(f.run.id),
                "review_token": view["review_token"],
            },
        )
    )
    assert result["review_decision"] == "approved"
    assert f.storage.repo.writes == 0


@pytest.mark.parametrize("content", [b"# Design\n" + b"x" * 32_000, b"x" * 64_001, b"\xff", None])
async def test_required_companion_checkpoint_uses_its_declared_limit(publication_db, content):
    f = await setup(publication_db)
    path = f.checkpoint.companions[0].artifact_path
    revision = f.storage.repo.edit({path: content})
    procedure = SimpleNamespace(
        companion_path=path,
        output_validator=None,
        documents=SimpleNamespace(companion_max_bytes=64_000),
        review_revision_context=None,
    )
    project = await f.db.get_project(f.run.project_id)
    if content is not None and 24_000 < len(content) <= 64_000:
        pair = await f.activities._procedure_companions(f.run, project, procedure, revision)
        assert pair[0].byte_count == len(content) and pair[0].ephemeral_commit_sha == revision
    else:
        with pytest.raises((ValueError, KeyError)):
            await f.activities._procedure_companions(f.run, project, procedure, revision)
    assert f.storage.repo.writes == 0
