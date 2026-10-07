"""Public tools use existing admission, without exposing the general MCP surface."""

# All credentials in this module are synthetic transport fixtures.
# ruff: noqa: S105, S106

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import SecretStr

from tin_lite.auth import AuthContext, ClerkAuth
from tin_lite.catalog import BUILTIN_WORKFLOWS, REGISTRY_REPO_ID
from tin_lite.domain import RunStatus
from tin_lite.mcp_public import SHARED_TOOLS, published_workflows, require_public_workflow
from tin_lite.mcp_server import create_mcp_app

RESOURCE = "https://tin.test/mcp/plugins"
ENTRY = next(w for w in BUILTIN_WORKFLOWS if w.key == "project.memory")


def workflow(entry=ENTRY, **changes):
    return SimpleNamespace(
        **{
            "id": entry.id,
            "project_id": None,
            "key": entry.key,
            "title": entry.title,
            "executor": entry.executor,
            "definition": deepcopy(entry.definition),
            "definition_repo_id": REGISTRY_REPO_ID,
            "definition_path": entry.definition_path,
            "current_commit_sha": "current-revision",
            **changes,
        }
    )


class FakeAuth:
    async def authenticate_oauth_token(self, token):
        if token not in {"plugin-token", "legacy-token"}:
            return None
        return AuthContext(
            clerk_user_id="user_fixture",
            token_type="oauth_token",
            scopes=frozenset({"openid"}),
            client_id="fixture-client",
            resource=RESOURCE if token == "plugin-token" else "https://tin.test/mcp",
        )


@pytest.fixture
async def fixture(monkeypatch):
    project = uuid4()
    token = SimpleNamespace(subject="user_fixture", scopes=["openid"], client_id="fixture-client")
    monkeypatch.setattr("tin_lite.mcp_server.get_access_token", lambda: token)
    db = SimpleNamespace(
        has_project_access=AsyncMock(return_value=True),
        record_tin_user=AsyncMock(),
        record_mcp_usage=AsyncMock(),
        list_projects_for_user=AsyncMock(return_value=[]),
        list_workflows=AsyncMock(return_value=[workflow()]),
        get_workflow=AsyncMock(return_value=workflow()),
        get_project_workflow=AsyncMock(),
        get_run_by_start_key=AsyncMock(return_value=None),
        get_run=AsyncMock(),
    )
    runtime = SimpleNamespace(database=db)
    settings = SimpleNamespace(
        switchboard_public_url="https://tin.test",
        clerk_frontend_api_url="https://clerk.tin.test",
        billing_enabled=True,
    )
    server, app = create_mcp_app(
        settings=settings, auth=FakeAuth(), runtime=lambda: runtime, public_plugin=True
    )
    run = SimpleNamespace(
        id=uuid4(),
        project_id=project,
        workflow_id=ENTRY.id,
        executor=ENTRY.executor,
        workflow_name=ENTRY.key,
        status=RunStatus.RUNNING,
        definition_commit_sha="old-revision",
    )
    db.get_run.return_value = run
    start = AsyncMock(return_value=run)
    monkeypatch.setattr("tin_lite.mcp_server.start_workflow_run", start)
    return SimpleNamespace(
        server=server,
        app=app,
        db=db,
        runtime=runtime,
        settings=settings,
        project=project,
        run=run,
        start=start,
        token=token,
    )


async def test_registered_surface_and_all_annotations(fixture):
    f = fixture
    tools = {t.name: t for t in await f.server.list_tools()}
    assert set(tools) == set(SHARED_TOOLS) | {
        w.public_mcp.name for w in published_workflows().values()
    }
    for t in tools.values():
        assert t.meta["securitySchemes"] == [{"type": "oauth2", "scopes": ["openid"]}]
        assert all(
            isinstance(v, bool)
            for v in (
                t.annotations.read_only_hint,
                t.annotations.destructive_hint,
                t.annotations.open_world_hint,
            )
        )
        assert t.input_schema["additionalProperties"] is False
    assert tools["start_project_memory"].input_schema["properties"]["inputs"] == {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
        "required": [],
    }
    legacy, _ = create_mcp_app(settings=f.settings, auth=FakeAuth(), runtime=lambda: f.runtime)
    assert {"start_workflow", "create_project", "create_billing_checkout"} <= {
        t.name for t in await legacy.list_tools()
    }


@pytest.mark.parametrize(
    "name",
    [
        "create_billing_checkout",
        "start_workflow",
        "start_project_workflow",
        "create_project",
        "create_personal_project",
        "get_started",
        "prepare_project_connection",
        "record_onboarding_picks",
        "create_project_workflow",
        "get_workflow",
        # Workflows hidden from discovery have no plugin tool.
        "start_visibility_audit",
        "start_error_surface_research",
        "start_mention_backlinks",
        "start_answer_page",
    ],
)
async def test_hidden_tools_cannot_be_called_by_name(fixture, name):
    with pytest.raises(ToolError, match="unsupported"):
        await fixture.server.call_tool(name, {})
    fixture.db.record_tin_user.assert_not_called()
    fixture.start.assert_not_called()


async def test_empty_account_never_provisions_or_records_new_user(fixture):
    result = await fixture.server.call_tool("list_projects", {})
    assert result.structured_content == {"result": []}
    fixture.db.record_tin_user.assert_not_called()


async def test_concrete_start_uses_shared_service_and_preserves_retry_pin(fixture):
    f = fixture
    request = str(uuid4())
    args = {"project_id": str(f.project), "request_id": request, "inputs": {}}
    result = await f.server.call_tool("start_project_memory", args)
    assert result.structured_content["id"] == str(f.run.id)
    assert "relay" not in result.structured_content
    call = f.start.call_args.kwargs
    assert call["workflow"].id == ENTRY.id
    assert call["input_payload"] == {}
    assert call["project_id"] == f.project
    assert call["start_idempotency_key"] == f"mcp:{request}"
    assert call["workflow"].current_commit_sha == "current-revision"
    assert "definition_commit_sha" not in call
    assert call["started_by_clerk_user_id"] == f.token.subject
    f.db.get_run_by_start_key.return_value = f.run
    retry = await f.server.call_tool("start_project_memory", args)
    assert retry.structured_content["already_started"] is True
    # The shared admission service recovers the original revision on retry.
    assert "definition_commit_sha" not in f.start.call_args.kwargs
    f.db.record_tin_user.assert_not_called()


async def test_saved_configuration_uses_existing_pinned_contract(fixture):
    f = fixture
    saved = SimpleNamespace(
        id=uuid4(),
        project_id=f.project,
        workflow_id=ENTRY.id,
        inputs={"project_id": str(f.project)},
        input_schema=ENTRY.definition["input_schema"],
        definition_commit_sha="saved-revision",
    )
    f.db.get_project_workflow.return_value = saved
    args = {
        "project_id": str(f.project),
        "request_id": str(uuid4()),
        "project_workflow_id": str(saved.id),
    }
    await f.server.call_tool("start_project_memory", args)
    call = f.start.call_args.kwargs
    assert call["input_payload"] == saved.inputs
    assert call["definition_commit_sha"] == "saved-revision"
    assert call["input_schema"] == saved.input_schema
    assert call["project_workflow_id"] == saved.id
    f.start.reset_mock()
    saved.workflow_id = uuid4()
    with pytest.raises(ToolError, match="unsupported"):
        await f.server.call_tool("start_project_memory", args)
    f.start.assert_not_called()


async def test_the_retired_technical_fix_has_no_public_tools(fixture):
    # organic.technical_fix is retired for new work: technical fixes go through
    # website.change, which this plugin does not expose.
    assert "organic.technical_fix" not in {w.key for w in published_workflows().values()}
    names = {tool.name for tool in await fixture.server.list_tools()}
    assert not names & {
        "preflight_technical_fix",
        "start_technical_fix",
        "stop_technical_fix",
        "list_technical_fix_sources",
        "get_technical_fix_source",
    }


@pytest.mark.parametrize(
    "extra",
    [
        {"workflow_id": "growth.onboarding"},
        {"inputs": {"project_id": str(uuid4())}},
        {"inputs": {"instruction": "run private code"}},
        {"billing_quote_id": str(uuid4())},
        {"request_id": "invalid"},
        {"project_workflow_id": str(uuid4())},
    ],
)
async def test_start_rejects_schema_escape_before_admission(fixture, extra):
    args = {"project_id": str(fixture.project), "request_id": str(uuid4()), "inputs": {}, **extra}
    with pytest.raises(ToolError, match="invalid"):
        await fixture.server.call_tool("start_project_memory", args)
    fixture.start.assert_not_called()


async def test_membership_is_required_before_start_or_run_reads(fixture):
    f = fixture
    f.db.has_project_access.return_value = False
    for name, args in [
        (
            "start_project_memory",
            {"project_id": str(f.project), "request_id": str(uuid4()), "inputs": {}},
        ),
        ("get_run", {"run_id": str(f.run.id)}),
    ]:
        with pytest.raises(ToolError, match="not_found"):
            await f.server.call_tool(name, args)
    f.start.assert_not_called()
    f.db.get_workflow.assert_not_called()


@pytest.mark.parametrize("key", ["growth.onboarding", "project.task"])
async def test_unpublished_run_cannot_be_read_or_approved(fixture, key):
    f = fixture
    excluded = next(w for w in BUILTIN_WORKFLOWS if w.key == key)
    f.db.get_workflow.return_value = workflow(excluded)
    for name in ("get_run", "read_run_output", "get_run_charge", "approve_workflow_run"):
        with pytest.raises(ToolError, match="unsupported"):
            await f.server.call_tool(name, {"run_id": str(f.run.id)})


@pytest.mark.parametrize(
    "changes",
    [
        {"project_id": uuid4()},
        {"key": "custom.memory"},
        {"definition_repo_id": "untrusted"},
        {"definition_path": "custom.json"},
        {"executor": "workflow.code"},
    ],
)
def test_catalog_identity_is_not_just_a_matching_id(changes):
    with pytest.raises(ToolError, match="unsupported"):
        require_public_workflow(workflow(**changes))


def test_schema_drift_is_rejected():
    changed = workflow()
    changed.definition["input_schema"]["properties"]["unexpected"] = {"type": "string"}
    with pytest.raises(ToolError, match="unsupported_revision"):
        require_public_workflow(changed)


async def test_billing_diagnostics_do_not_expose_checkout(fixture):
    f = fixture
    f.start.side_effect = ValueError("insufficient credits; call create_billing_checkout SECRET")
    with pytest.raises(ToolError, match="insufficient_funds") as error:
        await f.server.call_tool(
            "start_project_memory",
            {
                "project_id": str(f.project),
                "request_id": str(uuid4()),
                "inputs": {},
            },
        )
    assert "SECRET" not in str(error.value)
    assert "checkout" not in str(error.value)


async def test_tool_level_auth_challenge(fixture, monkeypatch):
    monkeypatch.setattr("tin_lite.mcp_server.get_access_token", lambda: None)
    result = await fixture.server.call_tool("list_projects", {})
    assert result.is_error
    assert 'error="invalid_token"' in result.meta["mcp/www_authenticate"][0]
    assert "/mcp/plugins" in result.meta["mcp/www_authenticate"][0]
    fixture.db.list_projects_for_user.assert_not_called()


async def test_http_discovery_tools_and_cross_resource_rejection(fixture):
    f = fixture
    async with f.app.router.lifespan_context(f.app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=f.app), base_url="https://tin.test"
        ) as client:
            metadata = await client.get("/.well-known/oauth-protected-resource/mcp/plugins")
            assert metadata.json()["resource"] == RESOURCE
            headers = {"Accept": "application/json, text/event-stream"}
            request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
            for token in (None, "legacy-token"):
                response = await client.post(
                    "/mcp/plugins",
                    json=request,
                    headers={
                        **headers,
                        **({"Authorization": f"Bearer {token}"} if token else {}),
                    },
                )
                assert response.status_code == 401
                assert "/mcp/plugins" in response.headers["www-authenticate"]
            response = await client.post(
                "/mcp/plugins",
                json=request,
                headers={
                    **headers,
                    "Authorization": "Bearer plugin-token",
                },
            )
            assert response.status_code == 200, response.text
            tools = response.json()["result"]["tools"]
            assert "start_project_memory" in {t["name"] for t in tools}
            assert "create_billing_checkout" not in {t["name"] for t in tools}
            assert all(t["_meta"]["securitySchemes"] for t in tools)


async def test_oauth_caches_and_legacy_policy_are_isolated(monkeypatch):
    monkeypatch.setattr("tin_lite.auth.time.time", lambda: 1_800_000_000)
    settings = SimpleNamespace(
        clerk_secret_key=SecretStr("synthetic-key"),
        clerk_jwt_key=None,
        clerk_authorized_parties=("https://tin.test",),
        mcp_oauth_client_ids=frozenset({"fixture-client"}),
        switchboard_public_url="https://tin.test",
        clerk_frontend_api_url="https://clerk.tin.test",
    )
    auths = [
        ClerkAuth(settings),
        ClerkAuth(
            settings,
            oauth_resource_path="/mcp/plugins",
            allow_legacy_clients=False,
        ),
    ]

    def response(request):
        import json

        token = json.loads(request.content)["access_token"]
        claims = (
            {}
            if token == "unbound"
            else {"aud": (RESOURCE if token == "public" else "https://tin.test/mcp")}
        )
        return httpx.Response(
            200,
            json={
                "subject": "user_fixture",
                "client_id": "fixture-client",
                "scopes": ["openid"],
                "revoked": False,
                "expired": False,
                "expiration": 1_800_000_300,
                **claims,
            },
        )

    try:
        for auth in auths:
            await auth.close()
            auth._client = httpx.AsyncClient(transport=httpx.MockTransport(response))  # noqa: SLF001
        legacy, public = auths
        # Verify repeatedly in both orders, including a populated success cache.
        for _ in range(2):
            assert await legacy.authenticate_oauth_token("legacy")
            assert await public.authenticate_oauth_token("legacy") is None
            assert await public.authenticate_oauth_token("public")
            assert await legacy.authenticate_oauth_token("public") is None
            assert await legacy.authenticate_oauth_token("unbound")
            assert await public.authenticate_oauth_token("unbound") is None
    finally:
        for auth in auths:
            await auth.close()


async def test_domain_challenge_is_optional_exact_plaintext():
    from fastapi import FastAPI

    from tin_lite.api import router

    app = FastAPI()
    app.include_router(router)
    app.state.settings = SimpleNamespace(openai_apps_challenge=None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://tin.test"
    ) as client:
        assert (await client.get("/.well-known/openai-apps-challenge")).status_code == 404
        app.state.settings.openai_apps_challenge = "synthetic-domain-proof"
        result = await client.get("/.well-known/openai-apps-challenge")
        assert result.status_code == 200
        assert result.text == "synthetic-domain-proof"
        assert result.headers["content-type"] == "text/plain; charset=utf-8"


async def test_actual_app_routes_both_resources_without_shadowing_consent(monkeypatch, fixture):
    # Import main using only synthetic settings; never load a developer's .env.
    import importlib
    import sys

    settings = SimpleNamespace(
        **vars(fixture.settings),
        clerk_secret_key=SecretStr("synthetic-key"),
        clerk_jwt_key=None,
        clerk_authorized_parties=("https://tin.test",),
        mcp_oauth_client_ids=frozenset(),
    )
    monkeypatch.setattr("tin_lite.settings.get_settings", lambda: settings)
    # Register the absence too, so teardown removes this synthetic module on a fresh import.
    monkeypatch.setitem(sys.modules, "tin_lite.main", None)
    monkeypatch.delitem(sys.modules, "tin_lite.main", raising=False)
    main = importlib.import_module("tin_lite.main")
    app = main.app
    app.state.settings = settings
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://tin.test"
    ) as client:
        for path in ("/mcp", "/mcp/plugins"):
            metadata = await client.get(f"/.well-known/oauth-protected-resource{path}")
            assert metadata.status_code == 200
            assert metadata.json()["resource"] == f"https://tin.test{path}"
            response = await client.post(
                path, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            )
            assert response.status_code == 401
            assert f"oauth-protected-resource{path}" in response.headers["www-authenticate"]
        # Authorize remains the existing Clerk redirect, not the public MCP app.
        response = await client.get("/mcp/authorize", params={"client_id": "fixture"})
        assert response.status_code in {302, 307}
        assert response.headers["location"].startswith("https://clerk.tin.test/")


async def test_public_lists_filter_private_workflows_and_do_not_return_admin_actions(fixture):
    from tin_lite.mcp_public import public_result

    entries = published_workflows()
    visible = {
        "id": str(uuid4()),
        "workflow_id": str(ENTRY.id),
        "name": "Saved memory",
        "inputs": {},
        "next_call": {"name": "create_billing_checkout"},
    }
    private = {"id": str(uuid4()), "workflow_id": str(uuid4()), "inputs": {"private": True}}
    result = public_result("list_project_workflows", {"result": [private, visible]}, entries)
    assert len(result["result"]) == 1
    assert result["result"][0]["id"] == visible["id"]
    assert "next_call" not in result["result"][0]
    text = "This user document mentions create_billing_checkout; preserve its actual text."
    result = public_result(
        "read_run_output",
        {"content": text, "document_handoff": {"tool": "start_workflow"}},
        entries,
    )
    assert result == {"content": text}


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Update brief [project-file:12345678-1234-4234-8234-123456789abc]", "Update brief"),
        ("Undo abcdef12 [project-file:12345678-1234-4234-8234-123456789abc]", "Undo abcdef12"),
        ("A normal commit", "A normal commit"),
        ("Document [project-file:example]", "Document [project-file:example]"),
        (
            "Explain [project-file:12345678-1234-4234-8234-123456789abc] in notes",
            "Explain [project-file:12345678-1234-4234-8234-123456789abc] in notes",
        ),
    ],
)
async def test_public_file_history_omits_internal_suffix_without_changing_storage(
    fixture, message, expected
):
    f = fixture
    row = {
        "revision": "a" * 40,
        "message": message,
        "author_name": "Synthetic reviewer",
        "date": datetime(2026, 1, 1, tzinfo=UTC),
        "state": "modified",
    }
    f.db.get_project = AsyncMock(
        return_value=SimpleNamespace(state_repo_id="fixture-repo", canonical_branch="main")
    )
    f.runtime.storage = SimpleNamespace(canonical_file_history=AsyncMock(return_value=[row]))
    args = {"project_id": str(f.project), "path": "notes/brief.md"}
    result = await f.server.call_tool("get_project_file_history", args)
    assert result.structured_content == {
        "result": [{**row, "message": expected, "date": row["date"].isoformat()}]
    }
    if message != expected:
        assert message not in result.content[0].text
    assert row["message"] == message
    legacy, _ = create_mcp_app(settings=f.settings, auth=FakeAuth(), runtime=lambda: f.runtime)
    legacy_result = await legacy.call_tool("get_project_file_history", args)
    assert legacy_result.structured_content["result"][0]["message"] == message


async def test_stop_reuses_normal_control_and_cannot_stop_a_private_run(fixture, monkeypatch):
    f = fixture
    stop = AsyncMock(return_value={"id": str(f.run.id), "status": "stopped"})
    monkeypatch.setattr("tin_lite.procedure_control.stop_procedure", stop)
    await f.server.call_tool("stop_procedure", {"run_id": str(f.run.id)})
    assert stop.call_args.kwargs["run_id"] == f.run.id
    stop.reset_mock()
    f.db.get_workflow.return_value = workflow(project_id=f.project, key="custom.memory")
    with pytest.raises(ToolError, match="unsupported"):
        await f.server.call_tool("stop_procedure", {"run_id": str(f.run.id)})
    stop.assert_not_called()


async def test_revision_delegates_the_exact_review_token_and_retry_key(fixture, monkeypatch):
    f = fixture
    article = next(w for w in BUILTIN_WORKFLOWS if w.key == "content.public_article")
    f.run.workflow_id = article.id
    f.run.executor = article.executor
    f.run.review_version = 2
    f.db.get_workflow.return_value = workflow(article)
    revised = SimpleNamespace(
        id=uuid4(), project_id=f.project, status=RunStatus.RUNNING, review_version=3
    )
    command = AsyncMock(return_value=revised)
    monkeypatch.setattr("tin_lite.workflow_reviews.WorkflowReviews.request_changes", command)
    f.runtime.storage = SimpleNamespace()
    request = str(uuid4())
    result = await f.server.call_tool(
        "request_workflow_changes",
        {
            "run_id": str(f.run.id),
            "feedback": "Shorten the opening.",
            "review_token": "exact-reviewed-version",
            "request_id": request,
        },
    )
    assert result.structured_content["run_id"] == str(revised.id)
    assert command.call_args.kwargs["token"] == "exact-reviewed-version"
    assert str(command.call_args.kwargs["request_id"]) == request
    assert command.call_args.kwargs["feedback"] == "Shorten the opening."
    assert command.call_args.kwargs["actor"] == f.token.subject


# Representative real inputs for the newly exposed contracts, including required
# fields rather than manufacturing inputs from the schema under test.
ADDED_INPUTS = {
    "style.capture": {"source_path": "style/sources/approved-samples.md"},
    "social.content_plan": {"hours_per_week": 2, "platforms": "linkedin"},
    "social.post_batch": {"mode": "repurpose", "article_path": "content/sample.md"},
    "brand.capture": {"product_url": "https://product.example", "include_repository": False},
    "product.analytics_brief": {"reporting_days": 7},
    "growth.score_quiz": {
        "product_name": "Fixture",
        "quiz_topic": "API readiness",
        "signup_url": "https://product.example/signup",
    },
    "competitor.watch": {"max_competitors": 2},
    "qa.buyer_trust": {"product_url": "https://product.example"},
    "outreach.paying_segment": {"retention_days": 60},
    "outreach.speaking_shortlist": {},
    "outreach.syllabus_placement": {},
    "outreach.marketplace_listings": {},
    "outreach.campus_events": {},
    "content.release_announce": {
        "changelog": "Added a CSV export for reports.",
        "product_name": "Fixture",
    },
    "outreach.awesome_lists": {},
    "outreach.community_threads": {},
    "outreach.newsletter_placements": {},
}


def test_reviewed_public_catalog_coverage_and_explicit_exclusions():
    from tin_lite.public_workflows import PUBLIC_WORKFLOWS

    entries = published_workflows()
    assert len(entries) == 43
    assert {w.key for w in PUBLIC_WORKFLOWS if w.id not in entries} == {
        "social.x_compose",
        "competitor.sunset_rescue",
        "growth.framework_starter",
        # Hidden from discovery, so hidden from the plugin too; saved configurations run.
        "organic.error_surface",
        "organic.mention_backlinks",
        # The organic loop's packages; no reviewed ChatGPT tools for them yet.
        "organic.traffic_snapshot",
        "organic.content_efficacy",
        "organic.site_architecture",
        "content.blog_index",
        "organic.prompt_panel",
    }
    assert {w.key for w in BUILTIN_WORKFLOWS if w.id not in entries} == {
        "connections.collect",
        "visibility.audit",
        "content.deliver",
        "website.change",
        "organic.technical_fix",
        "content.refresh",
        "content.answer_page",
        "social.x_revise",
        "social.x_draft",
        "social.x_style",
        "social.x_publish",
        "project.task",
        "growth.onboarding",
        "growth.onboarding_plan",
    }
    assert {w.key for w in PUBLIC_WORKFLOWS if w.id in entries} | {
        "style.capture",
    } == ADDED_INPUTS.keys()
    assert "growth.free_tool" not in {w.key for w in entries.values()}
    assert all(not w.key.startswith(("example.", "custom.")) for w in entries.values())


@pytest.mark.parametrize("key", ADDED_INPUTS)
@pytest.mark.parametrize("saved", [False, True], ids=["new-inputs", "saved-revision"])
async def test_added_workflow_starts_use_normal_admission(fixture, key, saved):
    f = fixture
    entry = next(w for w in published_workflows().values() if w.key == key)
    registered = workflow(entry)
    f.db.list_workflows.return_value = [registered]
    f.db.get_workflow.return_value = registered
    f.run.workflow_id, f.run.executor, f.run.workflow_name = entry.id, entry.executor, entry.key
    inputs = ADDED_INPUTS[key]
    arguments = {"project_id": str(f.project), "request_id": str(uuid4())}
    if saved:
        configured = SimpleNamespace(
            id=uuid4(),
            project_id=f.project,
            workflow_id=entry.id,
            inputs=inputs,
            input_schema=entry.definition["input_schema"],
            definition_commit_sha="saved-package-revision",
        )
        f.db.get_project_workflow.return_value = configured
        arguments["project_workflow_id"] = str(configured.id)
    else:
        arguments["inputs"] = inputs
    result = await f.server.call_tool(entry.public_mcp.name, arguments)
    assert result.structured_content["id"] == str(f.run.id)
    call = f.start.call_args.kwargs
    assert call["workflow"] is registered
    assert call["input_payload"] == inputs
    assert call["project_id"] == f.project
    if saved:
        assert call["definition_commit_sha"] == "saved-package-revision"
        assert call["input_schema"] == entry.definition["input_schema"]
    else:
        assert "definition_commit_sha" not in call
    f.db.record_tin_user.assert_not_called()


@pytest.mark.parametrize("key", ["social.content_plan", "brand.capture"])
async def test_public_package_identity_cannot_be_used_for_private_execution(fixture, key):
    f = fixture
    entry = next(w for w in published_workflows().values() if w.key == key)
    for changes in (
        {"project_id": f.project},
        {"key": "custom.copy"},
        {"definition_repo_id": "projects/private"},
        {"definition_path": "workflow_packages/custom.copy/workflow.json"},
    ):
        f.db.list_workflows.return_value = [workflow(entry, **changes)]
        with pytest.raises(ToolError, match="unsupported"):
            await f.server.call_tool(
                entry.public_mcp.name,
                {
                    "project_id": str(f.project),
                    "request_id": str(uuid4()),
                    "inputs": {},
                },
            )
    f.start.assert_not_called()


async def test_published_package_schemas_match_registry_publication():
    from tin_lite.public_workflows import load_public_workflows

    entries = published_workflows()
    for package in await load_public_workflows():
        if package.id not in entries:
            continue
        entry = entries[package.id]
        assert entry.definition == package.definition
        assert entry.definition_path == package.definition_path
        assert entry.executor == package.executor
        assert "public_mcp" not in package.definition


async def test_code_package_unknown_inputs_rejected_before_admission(fixture):
    with pytest.raises(ToolError, match="invalid"):
        await fixture.server.call_tool(
            "start_social_content_plan",
            {
                "project_id": str(fixture.project),
                "request_id": str(uuid4()),
                "inputs": {"python": "arbitrary user code"},
            },
        )
    fixture.start.assert_not_called()
