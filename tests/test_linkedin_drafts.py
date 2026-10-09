"""Draft-file boundaries use synthetic sources and no LinkedIn account."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from test_procedure_publication import publication_db as publication_db

from tin_lite.catalog import BUILTIN_WORKFLOWS, WORKFLOW_SYSTEMS
from tin_lite.linkedin_drafts import LinkedInDrafts, validate_draft


def draft():
    return {
        "schema_version": "tin.social.linkedin_draft.v1",
        "batch_id": "synthetic-batch",
        "author": "Ada",
        "audience": "Founders",
        "style_path": "",
        "brand_path": "brand/BRAND.md",
        "template_path": "social/linkedin/templates/library.json",
        "parent": None,
        "sources": [
            {
                "id": "s1",
                "label": "Release note",
                "author": "Ada",
                "use": "publishable",
                "excerpt": "We shipped a shared workspace.",
            }
        ],
        "posts": [
            {
                "id": "p1",
                "text": "We shipped a shared workspace.",
                "angle": "Shared context",
                "template_id": "lesson",
                "readiness": "ready",
                "support": [{"source_id": "s1", "excerpt": "We shipped a shared workspace."}],
                "editor_notes": "Check current availability.",
            }
        ],
        "gaps": [],
    }


def test_linkedin_group_preserves_existing_social_group():
    assert next(s for s in WORKFLOW_SYSTEMS if s.id == "x").name == "Social"
    assert next(s for s in WORKFLOW_SYSTEMS if s.id == "linkedin").name == "LinkedIn"
    assert next(w for w in BUILTIN_WORKFLOWS if w.key == "connections.collect").system == "linkedin"


def test_valid_draft_and_insufficient_evidence_artifact():
    assert validate_draft(json.dumps(draft()).encode())["posts"][0]["id"] == "p1"
    value = draft()
    value["posts"] = []
    value["gaps"] = [{"angle": "", "reason": "Add a publishable observation."}]
    assert not validate_draft(value)["posts"]


@pytest.mark.parametrize(
    "kind", ["private", "unknown", "inexact", "duplicate", "oversized", "control", "path", "empty"]
)
def test_rejects_invalid_drafts(kind):
    value = draft()
    if kind == "private":
        value["sources"][0]["use"] = "background"
    if kind == "unknown":
        value["posts"][0]["support"][0]["source_id"] = "missing"
    if kind == "inexact":
        value["posts"][0]["support"][0]["excerpt"] = "Made up quotation."
    if kind == "duplicate":
        value["posts"].append(deepcopy(value["posts"][0]))
    if kind == "oversized":
        value["posts"][0]["text"] = "a" * 2501
    if kind == "control":
        value["posts"][0]["text"] = "hidden\x00text"
    if kind == "path":
        value["style_path"] = "../private.md"
    if kind == "empty":
        value["posts"] = []
    with pytest.raises((ValueError, ValidationError)):
        validate_draft(value)


def runtime():
    path = "social/linkedin/drafts/test.json"
    return SimpleNamespace(
        database=SimpleNamespace(
            pool=SimpleNamespace(fetchval=AsyncMock(return_value=None)),
            has_project_access=AsyncMock(return_value=True),
            get_project=AsyncMock(
                return_value=SimpleNamespace(state_repo_id="repo", canonical_branch="main")
            ),
        ),
        storage=SimpleNamespace(
            list_canonical_files=AsyncMock(return_value=([path], "a" * 40)),
            read_bounded_project_file=AsyncMock(return_value=json.dumps(draft()).encode()),
        ),
        project_files=SimpleNamespace(
            commit=AsyncMock(return_value=SimpleNamespace(revision="b" * 40))
        ),
    ), path


@pytest.mark.asyncio
async def test_read_and_save_use_the_project_file_revision_guard():
    rt, path = runtime()
    service = LinkedInDrafts(rt)
    project = uuid4()
    view = await service.read(project, "member", path)
    value = view["draft"]
    value["posts"][0].update(text="An edited post.", readiness="edited")
    result = await service.save(
        project,
        "member",
        path=path,
        expected_revision=view["revision"],
        request_id=uuid4(),
        draft=value,
    )
    assert result["revision"] == "b" * 40
    call = rt.project_files.commit.call_args.kwargs
    assert call["expected_revision"] == "a" * 40
    assert call["actor_clerk_user_id"] == "member"
    assert len(call["changes"]) == 1
    rt.project_files.commit.side_effect = ValueError("revision conflict")
    with pytest.raises(ValueError, match="revision conflict"):
        await service.save(
            project,
            "member",
            path=path,
            expected_revision=view["revision"],
            request_id=uuid4(),
            draft=value,
        )


@pytest.mark.asyncio
async def test_revoked_or_cross_project_access_never_reads_or_writes():
    rt, path = runtime()
    rt.database.has_project_access.return_value = False
    service = LinkedInDrafts(rt)
    with pytest.raises(LookupError):
        await service.read(uuid4(), "outsider", path)
    with pytest.raises(LookupError):
        await service.save(
            uuid4(),
            "outsider",
            path=path,
            expected_revision="a" * 40,
            request_id=uuid4(),
            draft=draft(),
        )
    rt.storage.list_canonical_files.assert_not_called()
    rt.project_files.commit.assert_not_called()


@pytest.mark.asyncio
async def test_missing_file_and_other_channels_are_not_linkedin_drafts():
    rt, path = runtime()
    service = LinkedInDrafts(rt)
    with pytest.raises(ValueError):
        await service.read(uuid4(), "member", "social/x-drafts/test.json")
    rt.storage.list_canonical_files.return_value = ([], "a" * 40)
    with pytest.raises(LookupError):
        await service.read(uuid4(), "member", path)


@pytest.mark.asyncio
async def test_http_and_mcp_share_membership_and_conflict_checks(publication_db, monkeypatch):
    import httpx
    from test_private_workflows import ACTOR, app, fixture, mcp

    from tin_lite.project_files import ProjectFileService

    f = await fixture(publication_db)
    create_commit = f.storage.repo.create_commit

    def text_commit(**options):
        builder = create_commit(**options)
        builder.add_file_from_string = lambda path, text: builder.add_file(path, text.encode())
        return builder

    f.storage.repo.create_commit = text_commit
    f.storage.repo.list_commits = AsyncMock(
        side_effect=lambda **kwargs: {"commits": [f.storage.repo.commits[f.storage.repo.head]]}
    )
    f.runtime.project_files = ProjectFileService(database=f.db, storage=f.storage)
    path = "social/linkedin/drafts/example.json"
    f.storage.repo.edit({path: json.dumps(draft()).encode()})
    server = mcp(f, monkeypatch)
    base = f"/api/projects/{f.project.id}/linkedin/drafts"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        response = await client.get(base, params={"path": path})
        assert response.status_code == 200, response.text
        view = response.json()
        via_mcp = await server.call_tool(
            "read_linkedin_drafts", {"project_id": str(f.project.id), "path": path}
        )
        assert "We shipped a shared workspace." in str(via_mcp)
        assert "linkedin_draft" in str(via_mcp)
        edit = deepcopy(view["draft"])
        edit["posts"][0].update(text="An edited shared workspace post.", readiness="edited")
        request = {
            "path": path,
            "expected_revision": view["revision"],
            "request_id": str(uuid4()),
            "draft": edit,
        }
        save = await client.put(base, json=request)
        assert save.status_code == 200, save.text
        replay = await client.put(base, json=request)
        assert replay.status_code == 200 and replay.json() == save.json()
        request["request_id"] = str(uuid4())
        request["draft"]["posts"][0]["text"] = "Stale edit."
        stale = await client.put(base, json=request)
        assert stale.status_code == 409, stale.text
        await publication_db.pool.execute(
            "DELETE FROM project_memberships WHERE project_id=$1 AND clerk_user_id=$2",
            f.project.id,
            ACTOR,
        )
        assert (await client.get(base, params={"path": path})).status_code == 404
    with pytest.raises(Exception, match="[Pp]roject|access|member"):
        await server.call_tool(
            "read_linkedin_drafts", {"project_id": str(f.project.id), "path": path}
        )


def test_private_group_and_advanced_fields_are_presentation_only():
    from tin_lite.private_workflows import authoring_guide, validate_private_definition
    from tin_lite.workflow_inputs import validate_input_schema
    from tin_lite.workflow_packages import decode_workflow_source

    path = "workflow_packages/custom.research_digest/workflow.json"
    guide = authoring_guide(settings=SimpleNamespace(), project_id=uuid4())
    definition = decode_workflow_source(
        guide["example_files"][path].encode(), definition_path=path
    ).definition
    definition["system"] = "linkedin"
    validate_private_definition(definition)
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "project_id": {"type": "string", "format": "uuid"},
            "notes": {
                "type": "string",
                "maxLength": 200,
                "x-tin-ui": {"advanced": True, "control": "textarea"},
            },
        },
        "required": ["project_id"],
    }
    validate_input_schema(schema)
    schema["properties"]["notes"]["x-tin-ui"]["advanced"] = "yes"
    with pytest.raises(ValueError, match="boolean"):
        validate_input_schema(schema)


@pytest.mark.asyncio
async def test_revision_admits_the_pinned_private_package_once(publication_db, monkeypatch):
    import httpx
    from test_private_workflows import ACTOR, app, fixture, mcp

    from tin_lite.private_workflows import PackageActivation
    from tin_lite.workflow_code import example_files

    f = await fixture(publication_db)
    await f.db.upsert_workflow_system(system_id="linkedin", name="LinkedIn", display_order=9)
    files = example_files("custom.draft_fixture")
    manifest_path = "workflow_packages/custom.draft_fixture/workflow.json"
    manifest = json.loads(files[manifest_path])
    definition = manifest["definition"]
    definition["system"] = "linkedin"
    definition["code"]["output"].update(
        path="social/linkedin/drafts/{date}-{slug}.json",
        media_type="application/json",
        max_bytes=128000,
    )
    definition["input_schema"] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "project_id": {"type": "string", "format": "uuid"},
            "action": {"type": "string", "enum": ["draft", "revise"], "default": "draft"},
            **{
                key: {"type": "string", "maxLength": 4000}
                for key in ("draft_path", "draft_sha256", "post_id", "feedback")
            },
        },
        "required": ["project_id"],
    }
    files[manifest_path] = json.dumps(manifest)
    revision = f.storage.repo.edit({p: v.encode() for p, v in files.items()})
    active = await f.service.activate(
        project_id=f.project.id,
        actor=ACTOR,
        client_id=None,
        selection=PackageActivation(
            path=manifest_path, revision=revision, request_id=uuid4(), expected_revision=None
        ),
    )
    workflow_id = active["workflow_id"]
    original, _ = await f.db.create_run(
        project_id=f.project.id,
        workflow_id=workflow_id,
        started_by_clerk_user_id=ACTOR,
        definition_commit_sha=revision,
        pinned_definition=definition,
        input_payload={"project_id": str(f.project.id), "action": "draft"},
    )
    path = "social/linkedin/drafts/example.json"
    head = f.storage.repo.edit({path: json.dumps(draft()).encode()})
    await f.db.pool.execute(
        "UPDATE workflow_runs SET status='succeeded',artifact_path=$2,"
        "canonical_commit_sha=$3 WHERE id=$1",
        original.id,
        path,
        head,
    )
    service = LinkedInDrafts(f.runtime)
    view = await service.read(f.project.id, ACTOR, path)
    assert view["revision_source_run_id"] == str(original.id)
    # A later activation may change a recipe. Feedback keeps the producing revision.
    newer = deepcopy(manifest)
    newer["definition"]["description"] = "Changed current description"
    latest = f.storage.repo.edit({manifest_path: json.dumps(newer).encode()})
    await f.service.activate(
        project_id=f.project.id,
        actor=ACTOR,
        client_id=None,
        selection=PackageActivation(
            path=manifest_path, revision=latest, request_id=uuid4(), expected_revision=revision
        ),
    )
    body = {
        "path": path,
        "expected_sha256": view["sha256"],
        "post_id": "p1",
        "feedback": "Make the point clearer.",
        "request_id": str(uuid4()),
    }
    base = f"/api/projects/{f.project.id}/linkedin/drafts/revisions"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        response = await client.post(base, json=body)
        assert response.status_code == 202, response.text
        run = response.json()
        assert run["definition_commit_sha"] == revision
        assert (await f.db.get_run(UUID(run["id"]))).input["draft_sha256"] == view["sha256"]
        # Later parent edits do not prevent recovery of an already accepted request.
        changed = draft()
        changed["posts"][0]["text"] = "A later manual edit."
        f.storage.repo.edit({path: json.dumps(changed).encode()})
        replay = await client.post(base, json=body)
        assert replay.status_code == 202 and replay.json()["id"] == run["id"]
        server = mcp(f, monkeypatch)
        result = await server.call_tool(
            "revise_linkedin_draft", {"project_id": str(f.project.id), **body}
        )
        assert run["id"] in str(result)
        stale = await client.post(base, json={**body, "request_id": str(uuid4())})
        assert stale.status_code == 409
        conflict = await client.post(base, json={**body, "feedback": "Different feedback"})
        assert conflict.status_code == 422
    assert f.runtime.temporal.start_workflow.await_count == 1
