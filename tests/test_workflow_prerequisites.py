"""Declared prerequisites gate admission uniformly and are derived from durable project facts."""

import json
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from mcp.server.mcpserver.exceptions import ToolError
from test_procedure_publication import publication_db as publication_db
from test_workflow_definitions import ACTOR, activate_b, fixture, http_app, mcp

from tin_lite.activities import TinActivities
from tin_lite.api import router
from tin_lite.auth import AuthContext, require_user
from tin_lite.catalog import BUILTIN_WORKFLOWS, BuiltinWorkflow, sync_builtin_workflows
from tin_lite.domain import Workflow, WorkflowStatus
from tin_lite.mcp_server import create_mcp_app
from tin_lite.private_workflows import authoring_guide, validate_private_definition
from tin_lite.run_service import WorkflowExecutorUnavailableError, start_workflow_run
from tin_lite.system_wiki import SystemWikiRef
from tin_lite.workflow_packages import decode_workflow_source
from tin_lite.workflow_prerequisites import (
    PrerequisiteError,
    WorkflowPrerequisite,
    parse_workflow_prerequisites,
    project_readiness,
    validate_prerequisite_graph,
)

A = "a" * 40
HEAD = "c" * 40
PACKAGE = "workflow_packages/custom.research_digest/workflow.json"
FEATURE_MAP = (
    "# Project\n\n## Product\n\n### Feature map\n\n**Product**\n- A product.\n\n## Sources\n"
)


class FakeStorage:
    """A project-state HEAD with a fixed revision; counts reads for the bounded-call proof."""

    def __init__(self, files=None):
        self.files = dict(files or {})
        self.lists = 0
        self.reads = 0

    async def list_canonical_files(self, *, repo_id, branch):
        self.lists += 1
        return sorted(self.files), HEAD

    async def read_canonical_artifact(self, *, repo_id, commit_sha, path):
        self.reads += 1
        return self.files[path]


def structured(result):
    return result[1] if isinstance(result, tuple) else result.structured_content


def schema(**properties):
    return {"type": "object", "properties": {"project_id": {}, **properties}}


# ---------------------------------------------------------------- pure contract


@pytest.mark.parametrize(
    "value,message",
    [
        ({"kind": "run", "workflow": "qa.signup_walkthrough", "level": "required"}, "reason"),
        ({"kind": "gate", "level": "required", "reason": "x"}, "kind"),
        ({"kind": "run", "workflow": "a.b", "level": "soon", "reason": "x"}, "level"),
        ({"kind": "run", "workflow": "a.b", "level": "required", "reason": "x", "x": 1}, "fields"),
        ({"kind": "run", "workflow": "Bad Key", "level": "required", "reason": "x"}, "key"),
        (
            {
                "kind": "run",
                "workflow": "a.b",
                "match": ["host"],
                "level": "required",
                "reason": "x",
            },
            "scopes",
        ),
        (
            {
                "kind": "run",
                "workflow": "a.b",
                "via_input": "nope",
                "level": "required",
                "reason": "x",
            },
            "unknown input",
        ),
        (
            {
                "kind": "run",
                "workflow": "a.b",
                "max_age_days": 0,
                "level": "required",
                "reason": "x",
            },
            "max_age_days",
        ),
        ({"kind": "artifact", "path": "../x.md", "level": "required", "reason": "x"}, "unsafe"),
        (
            {"kind": "artifact", "path": "a/{missing}.md", "level": "required", "reason": "x"},
            "unknown",
        ),
        (
            {
                "kind": "artifact",
                "path": "a.md",
                "section": "### X",
                "level": "required",
                "reason": "x",
            },
            "wiki/INDEX.md",
        ),
        (
            {
                "kind": "identity",
                "reuse": "any",
                "host_input": "product_url",
                "level": "required",
                "reason": "x",
            },
            "active",
        ),
    ],
)
def test_parse_rejects_unknown_fields_levels_scopes_and_unsafe_paths(value, message):
    with pytest.raises(ValueError, match=message):
        parse_workflow_prerequisites([value], input_schema=schema(product_url={}, character={}))
    with pytest.raises(ValueError, match="duplicate"):
        parse_workflow_prerequisites(
            [
                {"kind": "run", "workflow": "a.b", "level": "required", "reason": "x"},
                {"kind": "run", "workflow": "a.b", "level": "recommended", "reason": "y"},
            ]
        )


def test_builtin_tree_is_acyclic_and_names_only_known_workflows():
    graph = {}
    for builtin in BUILTIN_WORKFLOWS:
        definition = builtin.definition
        graph[builtin.key] = parse_workflow_prerequisites(
            definition.get("prerequisites"), input_schema=definition["input_schema"]
        )
    validate_prerequisite_graph(graph)
    edges = {
        key: sorted((item.kind, item.level, item.upstream) for item in items)
        for key, items in graph.items()
        if items
    }
    assert edges["product.deep_dive"] == [
        ("artifact", "recommended", "product.code_map"),
        ("identity", "required", "qa.signup_walkthrough"),
        ("run", "required", "qa.signup_walkthrough"),
    ]
    assert edges["qa.product_audit"] == [
        ("artifact", "required", "product.deep_dive"),
        ("identity", "required", "qa.signup_walkthrough"),
        ("run", "required", "qa.signup_walkthrough"),
    ]
    assert edges["outreach.email_campaign"] == [
        ("artifact", "required", "outreach.email_shortlist")
    ]
    assert edges["content.plan"] == [
        ("run", "required", "organic.audit"),
        ("run", "required", "organic.keyword_plan"),
    ]
    assert edges["organic.technical_fix"] == [("run", "required", "organic.audit")]
    assert edges["creative.product_demo"] == [
        ("artifact", "recommended", "product.deep_dive"),
        ("artifact", "required", "creative.character"),
    ]
    assert all(level == "recommended" for _k, level, _u in edges["scan.report"])
    assert "qa.signup_walkthrough" not in edges and "organic.audit" not in edges


async def test_sync_fails_on_a_cycle_before_any_registry_write(monkeypatch):
    monkeypatch.setattr("tin_lite.public_workflows.PUBLIC_WORKFLOWS", ())

    def builtin(key, requires):
        return BuiltinWorkflow(
            id=uuid4(),
            key=key,
            title=key,
            description=key,
            executor="native.test",
            version_label="1.0.0",
            prerequisites=(
                WorkflowPrerequisite(
                    kind="run", level="required", reason="loop", workflow=requires
                ),
            ),
        )

    monkeypatch.setattr(
        "tin_lite.catalog.BUILTIN_WORKFLOWS", (builtin("a.one", "b.two"), builtin("b.two", "a.one"))
    )
    database = SimpleNamespace(
        get_workflow=AsyncMock(return_value=None),
        upsert_workflow_system=AsyncMock(),
        upsert_registry_workflow=AsyncMock(),
    )
    storage = SimpleNamespace(publish_workflow_files=AsyncMock())
    with pytest.raises(RuntimeError, match="cycle: a.one -> b.two -> a.one"):
        await sync_builtin_workflows(
            database=database,
            storage=storage,
            system_wiki=SystemWikiRef(repo_id="wiki", path="p.md", commit_sha=A),
        )
    storage.publish_workflow_files.assert_not_awaited()
    database.upsert_registry_workflow.assert_not_awaited()
    monkeypatch.setattr("tin_lite.catalog.BUILTIN_WORKFLOWS", (builtin("a.one", "z.gone"),))
    with pytest.raises(RuntimeError, match="unknown workflow z.gone"):
        await sync_builtin_workflows(
            database=database,
            storage=storage,
            system_wiki=SystemWikiRef(repo_id="wiki", path="p.md", commit_sha=A),
        )


# ---------------------------------------------------------------- project fixture


async def project_fixture(db, *, files=None):
    project = await db.create_project(name="Prereq proof", state_repo_id="projects/prereq")
    await db.record_tin_user(ACTOR)
    await db.grant_project_membership(project_id=project.id, clerk_user_id=ACTOR)
    workflows = {}
    for key in (
        "qa.signup_walkthrough",
        "product.code_map",
        "product.deep_dive",
        "qa.product_audit",
        "creative.character",
        "creative.product_demo",
        "research.deep_dive",
    ):
        builtin = next(w for w in BUILTIN_WORKFLOWS if w.key == key)
        await db.upsert_registry_workflow(
            workflow_id=builtin.id,
            key=builtin.key,
            executor=builtin.executor,
            title=builtin.title,
            description=builtin.description,
            definition_repo_id="registry/workflows",
            definition_path=builtin.definition_path,
            current_commit_sha=A,
            version_label=builtin.version_label,
            definition=builtin.definition,
        )
        workflows[key] = await db.get_workflow(builtin.id)
    storage = FakeStorage(files)
    settings = SimpleNamespace(
        billing_hosted_defaults_enabled=True,
        task_queue="prereq-proof",
        switchboard_public_url="https://tin.test",
        clerk_frontend_api_url="https://clerk.tin.test",
        private_workflow_projects=set(),
        fal_key="test-fal-key",
    )
    runtime = SimpleNamespace(
        database=db,
        storage=storage,
        temporal=SimpleNamespace(start_workflow=AsyncMock()),
        integrations=SimpleNamespace(ensure_requirements=AsyncMock()),
    )
    return SimpleNamespace(
        db=db,
        project=project,
        workflows=workflows,
        storage=storage,
        settings=settings,
        runtime=runtime,
    )


async def succeeded_run(f, key, inputs, *, identity_host=None):
    workflow = f.workflows[key]
    run, _ = await f.db.create_run(
        project_id=f.project.id,
        workflow_id=workflow.id,
        started_by_clerk_user_id=ACTOR,
        input_payload={"project_id": str(f.project.id), **inputs},
        definition_commit_sha=A,
        pinned_definition=workflow.definition,
    )
    await f.db.pool.execute(
        "UPDATE workflow_runs SET status='succeeded', canonical_commit_sha=$2, finished_at=now() "
        "WHERE id=$1",
        run.id,
        HEAD,
    )
    if identity_host is not None:
        await f.db.create_test_identity(
            identity_id=uuid4(),
            project_id=f.project.id,
            run_id=run.id,
            target_host=identity_host,
            label="tin",
            email=f"tin+{identity_host}@example.com",
            auth_kind="password",
            password_ciphertext=b"x" * 40,
            credential_key_version="v1",
        )
        await f.db.pool.execute(
            "UPDATE project_test_identities SET status='active', verified_at=now() "
            "WHERE created_by_run_id=$1",
            run.id,
        )
    return await f.db.get_run(run.id)


def start(f, key, inputs, **options):
    return start_workflow_run(
        runtime=f.runtime,
        settings=f.settings,
        workflow=f.workflows[key],
        project_id=f.project.id,
        started_by_clerk_user_id=ACTOR,
        input_payload=inputs,
        **options,
    )


def api(f):
    app = FastAPI()
    app.include_router(router)
    app.state.runtime, app.state.settings = f.runtime, f.settings
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id=ACTOR,
        token_type="session_token",  # noqa: S106 — token category, not a credential
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def server(f, monkeypatch):
    token = SimpleNamespace(subject=ACTOR, scopes=["openid"], client_id="test_client")
    monkeypatch.setattr("tin_lite.mcp_server.get_access_token", lambda: token)
    return create_mcp_app(settings=f.settings, auth=SimpleNamespace(), runtime=lambda: f.runtime)[0]


# ---------------------------------------------------------------- admission


async def test_scope_mismatch_signup_for_host_a_does_not_satisfy_deep_dive_for_host_b(
    publication_db,
):
    f = await project_fixture(publication_db)
    signup = await succeeded_run(
        f, "qa.signup_walkthrough", {"product_url": "https://a.example/"}, identity_host="a.example"
    )
    with pytest.raises(PrerequisiteError) as failure:
        await start(f, "product.deep_dive", {"product_url": "https://b.example/"})
    diagnostic = failure.value.diagnostic()
    assert diagnostic["code"] == "prerequisite_missing"
    unmet = {item["kind"]: item for item in diagnostic["prerequisites"] if not item["satisfied"]}
    assert set(unmet) == {"identity", "run", "artifact"}
    assert unmet["run"]["workflow_key"] == "qa.signup_walkthrough"
    assert unmet["run"]["workflow_id"] == str(f.workflows["qa.signup_walkthrough"].id)
    assert unmet["run"]["suggested_call"] == {
        "tool": "start_workflow",
        "workflow_id": "qa.signup_walkthrough",
        "inputs": {"product_url": "https://b.example/"},
    }
    assert "b.example" in unmet["identity"]["how_to_satisfy"]
    assert unmet["artifact"]["level"] == "recommended"
    assert f.runtime.temporal.start_workflow.await_count == 0
    run = await start(f, "product.deep_dive", {"product_url": "https://A.example/x"})
    evidence = run.prerequisite_evidence
    items = {item["kind"]: item for item in evidence["items"]}
    assert items["run"]["evidence"]["run_id"] == str(signup.id)
    assert items["run"]["evidence"]["canonical_commit_sha"] == HEAD
    assert items["identity"]["evidence"]["host"] == "a.example"
    assert items["artifact"]["satisfied"] is False
    assert [item["kind"] for item in evidence["advisories"]] == ["artifact"]
    assert evidence["advisories"][0]["suggested_call"]["workflow_id"] == "product.code_map"
    f.runtime.temporal.start_workflow.assert_awaited_once()


async def test_another_procedure_run_on_the_same_host_never_satisfies_the_signup_requirement(
    publication_db,
):
    f = await project_fixture(publication_db)
    # Every codex procedure shares one executor; only the workflow identity may satisfy a run.
    await succeeded_run(
        f, "qa.product_audit", {"product_url": "https://a.example/"}, identity_host="a.example"
    )
    with pytest.raises(PrerequisiteError) as failure:
        await start(f, "product.deep_dive", {"product_url": "https://a.example/"})
    unmet = [i["kind"] for i in failure.value.prerequisites if not i["satisfied"]]
    assert unmet == ["run", "artifact"]


async def test_http_and_mcp_return_the_same_409_prerequisite_diagnostic(
    publication_db, monkeypatch
):
    f = await project_fixture(publication_db)
    workflow = f.workflows["qa.product_audit"]
    async with api(f) as client:
        response = await client.post(
            f"/api/workflows/{workflow.id}/runs",
            json={"project_id": str(f.project.id), "inputs": {"product_url": "https://a.example/"}},
        )
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "prerequisite_missing"
    with pytest.raises(ToolError) as failure:
        await server(f, monkeypatch).call_tool(
            "start_workflow",
            {
                "project_id": str(f.project.id),
                "workflow_id": "qa.product_audit",
                "inputs": {"product_url": "https://a.example/"},
            },
        )
    text = str(failure.value)
    assert json.loads(text[text.index("{") :]) == detail
    assert sorted(item["workflow_key"] for item in detail["prerequisites"]) == [
        "product.deep_dive",
        "qa.signup_walkthrough",
        "qa.signup_walkthrough",
    ]
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM workflow_runs WHERE project_id=$1", f.project.id
        )
        == 0
    )
    f.runtime.temporal.start_workflow.assert_not_awaited()


async def test_studio_without_voice_configuration_stops_before_start(publication_db):
    f = await project_fixture(publication_db)
    f.settings.fal_key = None
    inputs = {"slug": "launch", "product_url": "https://a.example/"}
    with pytest.raises(WorkflowExecutorUnavailableError, match="Studio voice is not configured"):
        await start(f, "creative.product_demo", inputs)
    f.runtime.temporal.start_workflow.assert_not_awaited()
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 0

    f.settings.fal_key = "test-fal-key"
    admitted = await start(
        f, "creative.product_demo", inputs, start_idempotency_key="video-request"
    )
    f.settings.fal_key = None
    replayed = await start(
        f, "creative.product_demo", inputs, start_idempotency_key="video-request"
    )
    assert replayed.id == admitted.id


async def test_recommended_prerequisites_advise_and_placeholders_only_gate_when_given(
    publication_db, monkeypatch
):
    f = await project_fixture(publication_db)
    inputs = {"slug": "launch", "product_url": "https://a.example/"}
    started = structured(
        await server(f, monkeypatch).call_tool(
            "start_workflow",
            {
                "project_id": str(f.project.id),
                "workflow_id": "creative.product_demo",
                "inputs": inputs,
            },
        )
    )
    assert started["status"] == "pending"
    assert [a["kind"] for a in started["advisories"]] == ["artifact"]
    assert started["advisories"][0]["workflow_key"] == "product.deep_dive"
    run = await f.db.get_run(UUID(started["id"]))
    items = {i.get("path"): i for i in run.prerequisite_evidence["items"]}
    assert items["characters/{character}.svg"]["skipped"] is True
    with pytest.raises(PrerequisiteError, match="creative.character"):
        await start(f, "creative.product_demo", {**inputs, "character": "mascot"})
    f.storage.files["characters/mascot.svg"] = b"<svg/>"
    admitted = await start(
        f, "creative.product_demo", {**inputs, "slug": "again", "character": "mascot"}
    )
    evidence = {i.get("path"): i for i in admitted.prerequisite_evidence["items"]}
    assert evidence["characters/{character}.svg"]["evidence"] == {
        "path": "characters/mascot.svg",
        "revision": HEAD,
    }
    async with api(f) as client:
        view = (await client.get(f"/api/workflows/runs/{admitted.id}")).json()
    assert view["prerequisite_evidence"] == admitted.prerequisite_evidence
    mcp_view = structured(
        await server(f, monkeypatch).call_tool("get_run", {"run_id": str(admitted.id)})
    )
    assert mcp_view["prerequisite_evidence"] == admitted.prerequisite_evidence


async def test_feature_map_section_must_sit_under_the_product_parent(publication_db):
    f = await project_fixture(publication_db, files={"wiki/INDEX.md": b"# P\n\n### Feature map\n"})
    await succeeded_run(
        f, "qa.signup_walkthrough", {"product_url": "https://a.example/"}, identity_host="a.example"
    )
    with pytest.raises(PrerequisiteError) as failure:
        await start(f, "qa.product_audit", {"product_url": "https://a.example/"})
    assert [i["kind"] for i in failure.value.prerequisites if not i["satisfied"]] == ["artifact"]
    f.storage.files["wiki/INDEX.md"] = FEATURE_MAP.encode()
    run = await start(f, "qa.product_audit", {"product_url": "https://a.example/"})
    artifact = next(i for i in run.prerequisite_evidence["items"] if i["kind"] == "artifact")
    assert artifact["evidence"] == {
        "path": "wiki/INDEX.md",
        "revision": HEAD,
        "section": "### Feature map",
    }


async def test_idempotent_replay_skips_the_gate(publication_db, monkeypatch):
    f = await project_fixture(publication_db)
    signup = await succeeded_run(
        f, "qa.signup_walkthrough", {"product_url": "https://a.example/"}, identity_host="a.example"
    )
    options = {"start_idempotency_key": "same-request"}
    first = await start(f, "product.deep_dive", {"product_url": "https://a.example/"}, **options)
    await f.db.pool.execute("UPDATE workflow_runs SET status='failed' WHERE id=$1", signup.id)
    calls = []
    original = __import__(
        "tin_lite.run_service", fromlist=["evaluate_prerequisites"]
    ).evaluate_prerequisites

    async def counting(**kwargs):
        calls.append(kwargs["workflow"].key)
        return await original(**kwargs)

    monkeypatch.setattr("tin_lite.run_service.evaluate_prerequisites", counting)
    repeated = await start(f, "product.deep_dive", {"product_url": "https://a.example/"}, **options)
    assert repeated.id == first.id and calls == []
    with pytest.raises(PrerequisiteError):
        await start(f, "product.deep_dive", {"product_url": "https://a.example/"})
    assert calls == ["product.deep_dive"]


# ---------------------------------------------------------------- every dispatch surface


def with_prerequisite(definition):
    definition = deepcopy(definition)
    definition["prerequisites"] = [
        {
            "kind": "run",
            "workflow": "qa.signup_walkthrough",
            "level": "required",
            "reason": "Proof that every surface consults the same gate.",
        }
    ]
    return definition


@pytest.mark.parametrize("surface", ["http", "mcp", "schedule", "parent_child"])
async def test_every_dispatch_surface_consults_the_gate(publication_db, monkeypatch, surface):
    f = await fixture(publication_db)
    gated = with_prerequisite(f.definition)
    f.snapshots["a" * 40][f.builtin.definition_path] = json.dumps(gated).encode()
    await f.db.upsert_registry_workflow(**{**f.values, "definition": gated})
    latest = await activate_b(f)
    if surface == "http":
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=http_app(f)), base_url="http://test"
        ) as client:
            response = await client.post(
                f"/api/projects/{f.project.id}/workflows/{f.configured.id}/runs"
            )
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "prerequisite_missing"
    elif surface == "mcp":
        with pytest.raises(ToolError, match="prerequisite_missing"):
            await mcp(f, monkeypatch).call_tool(
                "start_project_workflow",
                {
                    "project_id": str(f.project.id),
                    "project_workflow_id": str(f.configured.id),
                    "request_id": str(uuid4()),
                },
            )
    elif surface == "schedule":
        activities = TinActivities(
            database=f.db, storage=f.storage, sandboxes=None, settings=f.settings
        )
        payload = await activities.dispatch_scheduled_workflow(
            {
                "project_workflow_id": str(f.configured.id),
                "scheduled_for": datetime.now(UTC).isoformat(),
                "occurrence_id": "gated-occurrence",
            }
        )
        assert payload == {}
        row = await f.db.pool.fetchrow(
            "SELECT status, error_message, prerequisite_evidence FROM workflow_runs "
            "WHERE project_workflow_id=$1",
            f.configured.id,
        )
        assert row["status"] == "failed"
        assert row["error_message"].startswith("Prerequisite missing: ")
        assert json.loads(row["prerequisite_evidence"])["items"][0]["satisfied"] is False
        saved = await f.db.get_project_workflow(f.configured.id)
        assert saved.next_run_at is not None
        return
    else:
        with pytest.raises(PrerequisiteError):
            await start_workflow_run(
                runtime=f.runtime,
                settings=f.settings,
                workflow=latest,
                project_id=f.project.id,
                started_by_clerk_user_id=ACTOR,
                input_payload=f.configured.inputs,
                project_workflow_id=f.configured.id,
                definition_commit_sha="a" * 40,
                input_schema=f.definition["input_schema"],
                _prepare_only=True,
            )
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM workflow_runs WHERE project_id=$1", f.project.id
        )
        == 0
    )
    f.runtime.temporal.start_workflow.assert_not_awaited()


async def test_schedule_retry_after_run_creation_still_consults_the_gate(publication_db):
    f = await fixture(publication_db)
    gated = with_prerequisite(f.definition)
    f.snapshots["a" * 40][f.builtin.definition_path] = json.dumps(gated).encode()
    await f.db.upsert_registry_workflow(**{**f.values, "definition": gated})
    await activate_b(f)
    activities = TinActivities(
        database=f.db, storage=f.storage, sandboxes=None, settings=f.settings
    )
    advance = f.db.advance_project_workflow_schedule
    failures = [ConnectionError("connection reset")]

    async def flaky_advance(**kwargs):
        if failures:
            raise failures.pop()
        return await advance(**kwargs)

    f.db.advance_project_workflow_schedule = flaky_advance
    payload = {
        "project_workflow_id": str(f.configured.id),
        "scheduled_for": datetime.now(UTC).isoformat(),
        "occurrence_id": "gated-retry-occurrence",
    }
    # The first attempt commits the run and then fails before its admission gate.
    with pytest.raises(ConnectionError):
        await activities.dispatch_scheduled_workflow(payload)
    assert await activities.dispatch_scheduled_workflow(payload) == {}
    row = await f.db.pool.fetchrow(
        "SELECT status, error_message FROM workflow_runs WHERE project_workflow_id=$1",
        f.configured.id,
    )
    assert row["status"] == "failed"
    assert row["error_message"].startswith("Prerequisite missing: ")


# ---------------------------------------------------------------- private packages


def example_definition():
    guide = authoring_guide(settings=SimpleNamespace(), project_id=uuid4())
    return decode_workflow_source(
        guide["example_files"][PACKAGE].encode(), definition_path=PACKAGE
    ).definition


@pytest.mark.parametrize(
    "prerequisites,accepted",
    [
        ([{"kind": "run", "workflow": "organic.audit", "level": "required", "reason": "x"}], True),
        (
            [{"kind": "artifact", "path": "reports/x.md", "level": "recommended", "reason": "x"}],
            True,
        ),
        (
            [
                {
                    "kind": "identity",
                    "reuse": "active",
                    "host_input": "brief",
                    "level": "required",
                    "reason": "x",
                }
            ],
            False,
        ),
        (
            [
                {
                    "kind": "artifact",
                    "path": "reports/x.md",
                    "producer": "scan.report",
                    "level": "required",
                    "reason": "x",
                }
            ],
            False,
        ),
        (
            [
                {
                    "kind": "run",
                    "workflow": "custom.research_digest",
                    "level": "required",
                    "reason": "x",
                }
            ],
            False,
        ),
        ([{"kind": "artifact", "path": ".env", "level": "required", "reason": "x"}], False),
    ],
)
def test_private_definitions_accept_runs_and_files_and_reject_identities_and_unsafe_paths(
    prerequisites, accepted
):
    definition = example_definition()
    definition["prerequisites"] = prerequisites
    if accepted:
        validate_private_definition(definition)
        assert authoring_guide(settings=SimpleNamespace(), project_id=uuid4())["capabilities"][
            "prerequisites"
        ]["levels"] == ["required", "recommended"]
    else:
        with pytest.raises(ValueError):
            validate_private_definition(definition)


# ---------------------------------------------------------------- listing readiness


def catalog_workflow(key):
    builtin = next(w for w in BUILTIN_WORKFLOWS if w.key == key)
    return Workflow(
        id=builtin.id,
        project_id=None,
        key=key,
        title=builtin.title,
        description=builtin.description,
        executor=builtin.executor,
        definition_repo_id="registry/workflows",
        definition_path=builtin.definition_path,
        current_commit_sha=A,
        version_label=builtin.version_label,
        definition=builtin.definition,
        status=WorkflowStatus.ACTIVE,
    )


async def test_listing_readiness_is_input_free_and_bounded():
    workflows = [catalog_workflow(b.key) for b in BUILTIN_WORKFLOWS]
    signup = catalog_workflow("qa.signup_walkthrough")
    identity = SimpleNamespace(
        target_host="a.example", status="active", auth_kind="password", password_ciphertext=b"x"
    )
    database = SimpleNamespace(
        list_prerequisite_runs=AsyncMock(
            return_value=[("qa.signup_walkthrough", SimpleNamespace(id=uuid4(), key=signup.key))]
        ),
        list_test_identities=AsyncMock(return_value=[identity]),
        get_project=AsyncMock(
            return_value=SimpleNamespace(state_repo_id="projects/x", canonical_branch="main")
        ),
    )
    storage = FakeStorage({"wiki/INDEX.md": FEATURE_MAP.encode()})
    readiness = await project_readiness(
        database=database, storage=storage, project_id=uuid4(), workflows=workflows
    )
    by_key = {w.key: readiness[w.id] for w in workflows}
    assert by_key["qa.signup_walkthrough"]["state"] == "ready"
    assert by_key["product.deep_dive"]["state"] == "advisory"
    assert [u["workflow_key"] for u in by_key["product.deep_dive"]["unmet"]] == ["product.code_map"]
    assert by_key["qa.product_audit"]["state"] == "ready"
    assert by_key["outreach.email_campaign"]["state"] == "blocked"
    blocked = by_key["outreach.email_campaign"]["unmet"][0]
    assert blocked["suggested_call"]["workflow_id"] == "outreach.email_shortlist"
    assert by_key["content.plan"]["state"] == "blocked"
    assert by_key["content.plan"]["unmet"][0]["scope"] == "checked_at_start"
    assert by_key["creative.product_demo"]["state"] == "ready"
    assert database.list_prerequisite_runs.await_count == 1
    assert database.list_test_identities.await_count == 1
    assert (storage.lists, storage.reads) == (1, 1)


async def test_list_workflows_and_get_workflow_expose_prerequisites_and_readiness(
    publication_db, monkeypatch
):
    f = await project_fixture(publication_db)
    listed = structured(
        await server(f, monkeypatch).call_tool("list_workflows", {"project_id": str(f.project.id)})
    )
    by_key = {item["key"]: item for item in listed["result"]}
    assert by_key["qa.signup_walkthrough"]["readiness"]["state"] == "ready"
    assert by_key["product.deep_dive"]["readiness"]["state"] == "blocked"
    assert by_key["product.deep_dive"]["prerequisites"][0]["kind"] == "identity"
    single = structured(
        await server(f, monkeypatch).call_tool(
            "get_workflow", {"project_id": str(f.project.id), "workflow_key": "qa.product_audit"}
        )
    )
    assert single["readiness"]["state"] == "blocked"
    async with api(f) as client:
        catalog = (await client.get(f"/api/workflows?project_id={f.project.id}")).json()
        one = (
            await client.get(
                f"/api/workflows/{f.workflows['product.deep_dive'].id}?project_id={f.project.id}"
            )
        ).json()
    assert {w["key"]: w["readiness"]["state"] for w in catalog}["product.deep_dive"] == "blocked"
    assert one["readiness"]["state"] == "blocked" and one["prerequisites"][0]["level"] == "required"
