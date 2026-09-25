from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from tin_lite.auth import AuthContext
from tin_lite.mcp_server import (
    MCP_SCOPE,
    ClerkOAuthTokenVerifier,
    _mcp_input_schema,
    _mcp_workflow,
    create_mcp_app,
)

VALID_CREDENTIAL = "valid-oauth-token"  # noqa: S105


class FakeOAuthAuth:
    async def authenticate_oauth_token(self, token: str) -> AuthContext | None:
        if token != VALID_CREDENTIAL:
            return None
        return AuthContext(
            clerk_user_id="user_Agent123",
            token_type="oauth_token",  # noqa: S106
            scopes=frozenset({MCP_SCOPE}),
            client_id="client_codex",
            expires_at=1_800_000_000,
            resource="https://tin.test/mcp",
        )


@pytest.mark.asyncio
async def test_clerk_oauth_token_maps_to_mcp_access_token() -> None:
    verifier = ClerkOAuthTokenVerifier(FakeOAuthAuth(), resource="https://tin.test/mcp")

    rejected = await verifier.verify_token("wrong")
    accepted = await verifier.verify_token(VALID_CREDENTIAL)

    assert rejected is None
    assert accepted is not None
    assert accepted.subject == "user_Agent123"
    assert accepted.client_id == "client_codex"
    assert accepted.scopes == [MCP_SCOPE]
    assert accepted.resource == "https://tin.test/mcp"


@pytest.mark.asyncio
async def test_mcp_publishes_discovery_and_rejects_anonymous_calls() -> None:
    settings = SimpleNamespace(
        switchboard_public_url="https://tin.test",
        clerk_frontend_api_url="https://clerk.tin.test",
    )
    _, app = create_mcp_app(
        settings=settings,
        auth=FakeOAuthAuth(),
        runtime=lambda: SimpleNamespace(),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://tin.test",
        ) as client:
            metadata = await client.get("/.well-known/oauth-protected-resource/mcp")
            anonymous = await client.post(
                "/mcp",
                headers={"Origin": "https://client.example"},
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            )

    assert metadata.status_code == 200
    assert metadata.json()["resource"] == "https://tin.test/mcp"
    assert metadata.json()["authorization_servers"] == ["https://clerk.tin.test"]
    assert MCP_SCOPE in metadata.json()["scopes_supported"]
    assert anonymous.status_code == 401
    assert anonymous.headers["www-authenticate"].startswith("Bearer ")
    assert anonymous.headers["access-control-allow-origin"] == "*"
    assert "WWW-Authenticate" in anonymous.headers["access-control-expose-headers"]


@pytest.mark.asyncio
async def test_mcp_exposes_personal_project_bootstrap_explicitly() -> None:
    settings = SimpleNamespace(
        switchboard_public_url="https://tin.test",
        clerk_frontend_api_url="https://clerk.tin.test",
        billing_enabled=True,
    )
    server, _ = create_mcp_app(
        settings=settings,
        auth=FakeOAuthAuth(),
        runtime=lambda: SimpleNamespace(),
    )

    tools = await server.list_tools()

    assert "create_personal_project" in {tool.name for tool in tools}
    assert "list_workspaces" in {tool.name for tool in tools}
    assert "create_project" in {tool.name for tool in tools}
    assert "approve_workflow_run" in {tool.name for tool in tools}
    assert "record_onboarding_picks" in {tool.name for tool in tools}
    assert "start_integration_connections" in {tool.name for tool in tools}
    assert "revise_email_campaign" in {tool.name for tool in tools}
    assert "approve_email_campaign_revision" in {tool.name for tool in tools}
    assert "discard_email_campaign_revision" in {tool.name for tool in tools}
    assert "list_project_workflows" in {tool.name for tool in tools}
    assert "create_project_workflow" in {tool.name for tool in tools}
    assert "update_project_workflow" in {tool.name for tool in tools}
    assert "start_project_workflow" in {tool.name for tool in tools}
    assert "read_run_output" in {tool.name for tool in tools}
    assert "compare_run_output" in {tool.name for tool in tools}
    assert "resolve_run_output" in {tool.name for tool in tools}
    assert "get_run_output_resolution" in {tool.name for tool in tools}
    assert "stop_organic_audit" in {tool.name for tool in tools}
    assert "stop_keyword_plan" in {tool.name for tool in tools}
    assert {
        "read_content_plan",
        "edit_content_plan",
        "revise_content_plan",
        "resolve_content_plan_revision",
    } <= {tool.name for tool in tools}
    assert "stop_content_plan" in {tool.name for tool in tools}
    assert {
        "list_technical_fix_sources",
        "get_technical_fix_source",
        "preflight_technical_fix",
    } <= {tool.name for tool in tools}
    assert {"stop_organic_system", "stop_technical_fix"} <= {tool.name for tool in tools}
    assert "stop_procedure" in {tool.name for tool in tools}
    assert "delete_project" in {tool.name for tool in tools}
    deletion = next(tool for tool in tools if tool.name == "delete_project")
    assert "confirm_name" in deletion.description
    assert "personal" in deletion.description
    assert "get_run_usage" in {tool.name for tool in tools}
    assert {
        "get_project_spending",
        "quote_workflow_run",
        "estimate_workflow_run",
        "get_run_charge",
        "set_project_spending_limits",
        "create_billing_checkout",
        "list_billing_payments",
        "enroll_billing_test",
    } <= {tool.name for tool in tools}
    assert "get_writing_style_guide" in {tool.name for tool in tools}
    assert "get_content_draft_sources" in {tool.name for tool in tools}
    assert "send_project_task_message" in {tool.name for tool in tools}
    assert {
        "get_content_delivery_settings",
        "save_content_delivery_settings",
        "retry_content_delivery",
    } <= {tool.name for tool in tools}
    assert {"get_workflow_review", "request_workflow_changes"} <= {tool.name for tool in tools}
    assert "set_project_hidden" not in {tool.name for tool in tools}
    project_list = next(tool for tool in tools if tool.name == "list_projects")
    assert "include_hidden" not in project_list.input_schema.get("properties", {})
    assert "start_integration_connections" in {tool.name for tool in tools}
    assert {
        "inspect_workflow_candidate",
        "qualify_workflow_package",
        "evaluate_workflow_case",
    } <= {tool.name for tool in tools}
    assert {"get_brand_guide", "get_brand"} <= {tool.name for tool in tools}
    assert len(tools) == 88
    assert "refund_billing_payment" not in {tool.name for tool in tools}
    start = next(tool for tool in tools if tool.name == "start_workflow")
    assert "instruction and title are only for project.task" in start.description
    assert "request_id" in start.description
    for tool in tools:
        if tool.name in {
            "list_technical_fix_sources",
            "get_technical_fix_source",
            "preflight_technical_fix",
            "get_run_usage",
            "inspect_workflow_candidate",
            "qualify_workflow_package",
        }:
            assert tool.annotations.read_only_hint is True


def test_mcp_workflow_contract_omits_bound_project_and_accepts_key_or_uuid() -> None:
    workflow = SimpleNamespace(id=uuid4(), key="research.deep_dive")
    schema = _mcp_input_schema(
        {
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "project_id": {"type": "string", "format": "uuid"},
                    "depth": {"type": "string"},
                },
                "required": ["project_id", "depth"],
            }
        }
    )

    assert "project_id" not in schema["properties"]
    assert schema["required"] == ["depth"]
    assert _mcp_workflow([workflow], "research.deep_dive") is workflow
    assert _mcp_workflow([workflow], str(workflow.id)) is workflow
    with pytest.raises(ToolError, match="UUID or key"):
        _mcp_workflow([workflow], "missing.workflow")


def test_server_instructions_open_with_the_offer_and_the_one_message_rule() -> None:
    from tin_lite.mcp_server import SERVER_INSTRUCTIONS

    opening = SERVER_INSTRUCTIONS[:512]  # Codex prioritizes the first 512 characters
    assert "First turn:" in opening and "ask\nfor a go-ahead before calling anything" in opening
    assert "Every business gets its own project" in opening
    # Two founder-facing fields, treated apart: quote as given, relay in the agent's words.
    assert "`quote` is Tin's own words" in SERVER_INSTRUCTIONS
    assert "`relay` is a list of facts" in SERVER_INSTRUCTIONS
    assert "Never quote a relay item, and never paraphrase a quote." in SERVER_INSTRUCTIONS
    assert "`tell_the_founder` is the two joined for older clients" in SERVER_INSTRUCTIONS
    assert "word for word" not in SERVER_INSTRUCTIONS
    # The plan's minutes: one optional question for context and connections.
    assert "start_workflow's `meanwhile`" in SERVER_INSTRUCTIONS
    assert "`context/<slug>.md`" in SERVER_INSTRUCTIONS
    assert "never open files they did not name" in SERVER_INSTRUCTIONS
    assert "Guess how the founder sees the project" in SERVER_INSTRUCTIONS
    assert "set up whole systems" in SERVER_INSTRUCTIONS
    assert "default to side and signups" in SERVER_INSTRUCTIONS
    assert "refuses growth.onboarding" not in SERVER_INSTRUCTIONS
    assert "What do you want Tin to take on?" in SERVER_INSTRUCTIONS
    assert "AskUserQuestion in Claude Code" in SERVER_INSTRUCTIONS
    assert "request_user_input in Codex" in SERVER_INSTRUCTIONS
    assert "ONE open question" not in SERVER_INSTRUCTIONS
    assert "expand the scope" not in SERVER_INSTRUCTIONS
    assert "A to D" not in SERVER_INSTRUCTIONS
    assert "get_started return them as `links`" not in SERVER_INSTRUCTIONS


def test_missing_founder_answers_become_stated_defaults() -> None:
    from tin_lite.mcp_server import FOUNDER_DEFAULTS, _assumed_line, _founder_answers_missing

    assert _founder_answers_missing({"priority": "unknown"}) == ["priority", "outcome"]
    assert _founder_answers_missing({"priority": "main", "outcome": "launch"}) == []
    assert FOUNDER_DEFAULTS == {"priority": "side", "outcome": "signups"}
    line = _assumed_line(dict(FOUNDER_DEFAULTS))
    assert line.startswith("I set this up as a serious side project aiming for more signups.")
    assert "tell me and I will adjust" in line
    assert _assumed_line({"outcome": "launch"}).startswith(
        "I set this up as a project aiming for a launch"
    )


def test_posthog_mcp_analytics_installs_without_touching_the_tool_contract(monkeypatch) -> None:
    import posthog.mcp

    from tin_lite.mcp_server import install_posthog_mcp_analytics

    calls: list[tuple] = []
    monkeypatch.setattr(posthog.mcp, "instrument", lambda *args: calls.append(args))
    server = object()
    assert install_posthog_mcp_analytics(server, SimpleNamespace(posthog_api_key=None)) is None
    client = install_posthog_mcp_analytics(
        server, SimpleNamespace(posthog_api_key="phc_test", posthog_host="https://eu.i.posthog.com")
    )
    try:
        assert client is not None and calls and calls[0][0] is server and calls[0][1] is client
        options = calls[0][2]
        # No injected `context` argument, no virtual feedback tool, no duplicate exceptions.
        assert options.context is False and options.collect_feedback is False
        assert options.enable_exception_autocapture is False
        assert options.identify(None, None) is None  # no access token outside a request
        assert options.event_properties(None, None)["source"] == "tin-lite"
    finally:
        client.shutdown()
