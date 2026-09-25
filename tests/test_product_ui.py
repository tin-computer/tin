from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from tin_lite.api import router
from tin_lite.auth import AuthContext, require_user
from tin_lite.db import Database
from tin_lite.domain import (
    ActivityEvent,
    ChatMessage,
    Project,
    RunStatus,
    Workflow,
    WorkflowRun,
    WorkflowStatus,
)


def product_state() -> tuple[Project, Workflow, WorkflowRun, ActivityEvent, ChatMessage]:
    project = Project(uuid4(), "Tin POC", "projects/tin-poc", "main")
    workflow = Workflow(
        id=uuid4(),
        project_id=None,
        key="scan.report",
        title="Scan project",
        description="Publish SCAN.md.",
        executor="scan.report",
        definition_repo_id="registry/workflows",
        definition_path="workflows/scan.report.json",
        current_commit_sha="d" * 40,
        version_label="1.0.0",
        definition={},
        status=WorkflowStatus.ACTIVE,
        system_id="organic-traffic",
        system_name="Organic traffic system",
        system_order=1,
    )
    run = WorkflowRun(
        id=uuid4(),
        project_id=project.id,
        workflow_id=workflow.id,
        executor=workflow.executor,
        definition_commit_sha=workflow.current_commit_sha,
        temporal_workflow_id=f"scan.report:{uuid4()}",
        thread_id=str(workflow.id),
        generation=1,
        fencing_token=1,
        status=RunStatus.SUCCEEDED,
        artifact_path="reports/SCAN.md",
    )
    event = ActivityEvent(
        id=1,
        project_id=project.id,
        run_id=run.id,
        event_type="scan_report_ready",
        details={"artifact_ref": "code.storage://durable"},
        summary="Project scan report is ready.",
        audience="product",
        created_at=datetime.now(UTC),
        workflow_key=workflow.key,
        workflow_title=workflow.title,
    )
    message = ChatMessage(
        id=uuid4(),
        project_id=project.id,
        request_id=uuid4(),
        role="user",
        source="founder",
        content="What should we scan?",
        author_clerk_user_id="user_test",
        response_id=None,
        routed_workflow_key=None,
        run_id=None,
        created_at=datetime.now(UTC),
    )
    return project, workflow, run, event, message


@pytest.mark.asyncio
async def test_pending_decisions_decode_asyncpg_jsonb_text() -> None:
    project_id = uuid4()

    class TextJsonPool:
        async def fetch(self, query, selected_project_id):
            assert "FROM workflow_runs AS run" in query
            assert selected_project_id == project_id
            return [
                {
                    "id": uuid4(),
                    "run_id": uuid4(),
                    "items": '[{"id":"draft","title":"Draft"}]',
                    "response_schema": '{"type":"object"}',
                }
            ]

    database = Database("postgresql://unused")
    database._pool = TextJsonPool()  # type: ignore[assignment]

    decisions = await database.list_pending_decisions(project_id=project_id)

    assert decisions[0]["items"] == [{"id": "draft", "title": "Draft"}]
    assert decisions[0]["response_schema"] == {"type": "object"}


@pytest.mark.asyncio
async def test_product_ui_reads_projects_runs_and_activity_from_postgres() -> None:
    project, workflow, run, event, message = product_state()
    start_here = replace(
        workflow,
        id=uuid4(),
        key="growth.onboarding",
        title="Start here: onboard this business",
        executor="growth.onboarding",
        definition_path="workflows/growth.onboarding.json",
        definition={"agent_only": True},
        system_id="start-here",
        system_name="Start here",
        system_order=0,
    )

    class ProductDatabase:
        async def list_projects_for_user(self, clerk_user_id):
            assert clerk_user_id == "user_test"
            return [project]

        async def has_project_access(self, *, project_id, clerk_user_id):
            return project_id == project.id and clerk_user_id == "user_test"

        async def get_project(self, project_id):
            return project if project_id == project.id else None

        async def list_runs(self, *, project_id, limit):
            assert project_id == project.id
            assert limit == 25
            return [run]

        async def list_product_activity(self, *, project_id, limit, offset):
            assert project_id == project.id
            assert limit == 25
            assert offset == 0
            return [event]

        async def list_chat_messages(self, *, project_id, limit):
            assert project_id == project.id
            assert limit == 25
            return [message]

        async def list_workflows(self, *, project_id=None):
            return [workflow, start_here]

        async def get_workflow_template_state(self, *, project_id, clerk_user_id):
            assert project_id == project.id
            assert clerk_user_id == "user_test"
            return {workflow.id: {"saved": True, "project_workflow_count": 2}}

        async def list_pending_decisions(self, *, project_id):
            assert project_id == project.id
            return []

        async def get_project_system_summary(self, *, project_id):
            assert project_id == project.id
            return {
                "workflow_count": 2,
                "running_count": 1,
                "runs_this_month": 7,
                "waiting_count": 1,
                "activity_count_30_days": 9,
                "next_run_at": datetime(2026, 9, 5, 16, 0, tzinfo=UTC),
                "timezone": "America/Los_Angeles",
            }

    class TemporalMustNotBeRead:
        def __getattr__(self, name):
            raise AssertionError(f"product UI read touched Temporal: {name}")

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id="user_test",
        token_type="session_token",  # noqa: S106
        session_id="sess_test",
    )
    app.state.settings = SimpleNamespace(
        clerk_publishable_key="pk_test_test",
        clerk_frontend_api_url="https://clerk.test",
        switchboard_public_url="http://test",
    )
    app.state.runtime = SimpleNamespace(
        database=ProductDatabase(),
        temporal=TemporalMustNotBeRead(),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        landing = await client.get("/")
        sign_in = await client.get("/sign-in")
        sign_up = await client.get("/sign-up")
        projects = await client.get("/api/projects")
        system = await client.get(f"/api/projects/{project.id}/system")
        runs = await client.get(f"/api/projects/{project.id}/runs?limit=25")
        activity = await client.get(f"/api/projects/{project.id}/activity?limit=25")
        decisions = await client.get(f"/api/projects/{project.id}/decisions")
        messages = await client.get(f"/api/projects/{project.id}/chat/messages?limit=25")
        workflows = await client.get(f"/api/workflows?project_id={project.id}")

    assert landing.status_code == 200
    assert '<main id="main"' in landing.text
    assert 'id="auth-view"' in landing.text
    assert sign_in.status_code == 200
    assert sign_up.status_code == 200
    assert 'id="auth-view"' in sign_in.text
    assert 'id="auth-view"' in sign_up.text
    assert "{{ASSET_VERSION}}" not in landing.text
    assert "/assets/app.js?v=" in landing.text
    assert landing.headers["Cache-Control"] == "no-store, max-age=0"
    assert projects.headers["X-Tin-Read-Source"] == "postgres"
    assert projects.json()[0]["id"] == str(project.id)
    assert system.headers["X-Tin-Read-Source"] == "postgres"
    assert system.json() == {
        "workflow_count": 2,
        "running_count": 1,
        "runs_this_month": 7,
        "waiting_count": 1,
        "activity_count_30_days": 9,
        "next_run_at": "2026-09-05T16:00:00Z",
        "set_up_at": None,
        "timezone": "America/Los_Angeles",
        "last_mcp_used_at": None,
        "last_mcp_tool_name": None,
    }
    assert runs.headers["X-Tin-Read-Source"] == "postgres"
    assert runs.json()[0]["artifact_path"] == "reports/SCAN.md"
    assert activity.headers["X-Tin-Read-Source"] == "postgres"
    assert activity.json()[0]["workflow_key"] == "scan.report"
    assert decisions.headers["X-Tin-Read-Source"] == "postgres"
    assert decisions.json() == []
    assert messages.headers["X-Tin-Read-Source"] == "postgres"
    assert messages.json()[0]["content"] == "What should we scan?"
    assert workflows.headers["X-Tin-Read-Source"] == "postgres"
    # Agent-only (start here) workflows never reach the product catalog.
    assert [item["key"] for item in workflows.json()] == ["scan.report"]
    assert workflows.json()[0]["system_id"] == "organic-traffic"
    assert workflows.json()[0]["system_name"] == "Organic traffic system"
    assert workflows.json()[0]["system_order"] == 1
    assert workflows.json()[0]["saved"] is True
    assert workflows.json()[0]["project_workflow_count"] == 2


def test_product_ui_assets_are_packaged_beside_the_application() -> None:
    static = Path(__file__).parents[1] / "src" / "tin_lite" / "static"
    script = (static / "app.js").read_text() + (static / "auth-appearance.js").read_text()
    diagram_loader = (static / "diagram-loader.js").read_text()
    diagram_script = (static / "diagram-renderer.js").read_text()
    delimited_script = (static / "delimited-viewer.js").read_text()
    viewer_script = (static / "markdown-viewer.js").read_text()
    trees_script = (static / "pierre-trees.js").read_text()
    theme_script = (static / "theme.js").read_text()
    stylesheet = (static / "app.css").read_text()
    comparison_stylesheet = (static / "output-comparison.css").read_text()
    api_source = (static.parent / "api.py").read_text()
    logo = static / "tin-logotype-ink.png"

    index = (static / "index.html").read_text()
    viewer_index = (static / "viewer.html").read_text()
    assert "Paper" not in index
    assert '<img src="/assets/tin-logotype-ink.png" alt="Tin" />' in index
    assert (
        '<link rel="icon" href="/assets/tin-favicon.svg?v={{ASSET_VERSION}}" '
        'type="image/svg+xml" />' in index
    )
    assert 'window.localStorage.getItem("tin-lite:theme")' in index
    assert index.index("document.documentElement.dataset.theme = theme") < index.index(
        '<link rel="stylesheet" href="/assets/app.css'
    )
    assert 'window.localStorage.getItem("tin-lite:theme")' in viewer_index
    assert '<script src="/assets/theme.js?v={{ASSET_VERSION}}" defer></script>' in viewer_index
    assert '<meta name="theme-color" content="#f7f5ef" />' in viewer_index
    assert hashlib.sha256(logo.read_bytes()).hexdigest() == (
        "0317c2b084ce52582886309648b763d80f6a5aa72e1cdd04e61af8ac66b44be4"
    )
    assert "--paper-bg: #f7f5ef" in stylesheet
    assert "--ink-rgb: 38 35 26" in stylesheet
    assert "--card-border: rgb(var(--ink-rgb) / 14%)" in stylesheet
    assert "--well-ink: #26231c" in stylesheet
    assert 'html[data-theme="dark"] {' in stylesheet
    assert "--paper-bg: #141210" in stylesheet
    assert "--card-border: rgb(246 241 231 / 8%)" in stylesheet
    assert "--card-hairline: rgb(246 241 231 / 8%)" in stylesheet
    assert "--hot-wash: rgb(var(--hot-rgb) / 12%)" in stylesheet
    assert "--scrim: rgb(0 0 0 / 55%)" in stylesheet
    assert "var(--paper)" not in stylesheet
    assert "rgb(38 35 26 /" not in stylesheet
    assert "rgb(38 35 26 /" not in comparison_stylesheet
    assert "border: 1px solid var(--card-border)" in comparison_stylesheet
    assert "border: var(--card-border-width) solid var(--card-border)" in stylesheet
    assert "margin-inline: calc(var(--card-radius) - var(--card-border-width))" in stylesheet
    assert stylesheet.count("border: 1px solid var(--card-border)") >= 9
    assert "border-top: 1px solid var(--card-hairline)" in stylesheet
    assert 'html[data-theme="dark"] .project-switcher,' in stylesheet
    assert 'html[data-theme="dark"] .search-field input,' in stylesheet
    assert "border-color: var(--paper-border-strong)" in stylesheet
    assert "background: var(--diff-added-bg)" in comparison_stylesheet
    assert ".status-dot.is-ready" in stylesheet
    assert 'data-workflow-section="yours"' in script
    assert 'data-workflow-section="registry"' in script
    assert 'placeholder="Search…"' in script
    assert 'data-workflow-section="yours">My system' in script
    assert 'data-workflow-section="registry">Add workflows' in script
    assert "function systemMySystemHtml()" in script
    # The week ahead (Paper SYS-V3) sits above the Scheduled cards.
    assert "function systemWeekAheadHtml()" in script
    assert "set up by your coding agent" in script
    assert '<span class="is-quiet">quiet</span>' in script
    assert "Then every week: " in script
    assert ".system-week-days {" in (static / "app.css").read_text()
    assert 'systemWorkflowGroup("Running", runningCards)' in script
    assert "saved ${workflowCountValue === 1" in script
    assert "function systemRunDetailHtml(run, includeClose = true)" in script
    assert "function emailCampaignRunDetail(run, detail)" in script
    assert "/api/outreach/campaigns/${encodeURIComponent(runId)}/deliveries" in script
    assert 'data-observe-run="${escapeHtml(run.id)}"' in script
    assert 'data-observe-event="${escapeHtml(event.id)}"' in script
    assert "function systemProgressBar(run)" in script
    assert 'waiting: "waiting"' in script
    assert "`${phase} · ${run.progress_current} of ${run.progress_total}`" in script
    assert 'data-editor-run-id="${escapeHtml(run.id)}"' in script
    assert "return systemProjectWorkflowEditor(workflow, configured, run);" in script
    assert "data-open-system-workflow" in script
    assert "data-close-system-workflow" in script
    assert "function retryProjectWorkflow(projectWorkflowId, button)" in script
    assert "/retry`" in script
    assert (
        'class="system-progress ${determinate ? "is-determinate" : "is-indeterminate"}"' in script
    )
    assert "api(`/api/projects/${projectId}/system`)" in script
    assert ".system-workflow-card" in stylesheet
    system_card_row_css = stylesheet[
        stylesheet.index(".system-card-row {") : stylesheet.index(
            ".system-card-row.is-configurable"
        )
    ]
    assert "min-height" not in system_card_row_css
    assert (
        "width: 420px;\n  min-width: 0;\n  flex: 0 0 420px;\n  align-items: baseline;" in stylesheet
    )
    assert ".system-card-identity code {\n    display: none;\n  }" in stylesheet
    assert "height: 3px" in stylesheet
    assert ".agent-rail-body pre::-webkit-scrollbar-track" in stylesheet
    assert "scrollbar-color: rgb(var(--on-dark-rgb) / 18%) transparent" in stylesheet
    assert "schedule_mode" in script
    assert "tinSegmentedControl" in script
    assert "tinSelectControl" in script
    assert "api(`/api/projects/${projectId}/integrations`)" in script
    assert "No product integrations yet" not in script
    assert "data-integration-connect" in script
    assert 'new Set(["analytics.gsc", "infra.github", "analytics.posthog"])' in script
    assert 'id="integration-project-dialog"' in index
    assert 'role="radiogroup"' in index
    assert "function chooseIntegrationProject(providerKey, capabilities)" in script
    assert "Connections are project-owned. Choose the Tin project" in script
    assert "integrationProjectDialog.showModal()" in script
    assert "targetProjectId || context.projectId" in script
    assert "function promptForIntegrationResource(providerKey)" in script
    assert "OAuth is connected, but workflows cannot use" in script
    assert 'needsResource ? "Finish setup" : "Configure"' in script
    assert (
        "if (connectedProvider) await promptForIntegrationResource(connectedProvider)" not in script
    )
    assert "return connected.key" not in script
    # The callback lands on the project that owns the new connection.
    assert "project_id: UUID | None = None" in api_source
    assert 'project_id=getattr(connection, "project_id", None)' in api_source
    assert "return { provider: connected.key, projectId: connected.project_id || null }" in script
    assert "clearIntegrationCallbackUrl(connected.project_id)" in script
    assert "clearIntegrationCallbackUrl(error.detail.project_id)" in script
    assert "function clearIntegrationCallbackUrl(projectId = null)" in script
    assert 'if (projectId) callbackUrl.searchParams.set("project", projectId)' in script
    assert (
        "await bootstrap(invitedProject?.id || integrationReturn?.projectId || null, "
        "integrationReturn)" in script
    )
    assert (
        "if (connection && connection.projectId === state.project?.id) "
        "await promptForIntegrationResource(connection.provider)" in script
    )
    assert 'new URL(location.href).searchParams.get("project") || storedProjectId()' in script
    assert (
        "GitHub returned without the connection state. Start the connection again from the project."
        in script
    )
    assert "project_id: stateToken ? null : callbackProjectId()" in script
    # Repository picker as a dialog right after the connect (Search Console keeps the scroll).
    assert "async function chooseGitHubRepository()" in script
    assert "Choose the repository for ${state.project?.name" in script
    assert "/integrations/infra.github/options`" in script
    assert 'if (providerKey === "infra.github") {\n    await chooseGitHubRepository();' in script
    assert "Only repositories the Tin app is installed on appear here." in script
    assert 'href="${GITHUB_INSTALLATIONS_URL}"' in script
    assert 'GITHUB_INSTALLATIONS_URL = "https://github.com/settings/installations"' in script
    assert "if (state.repositoryChoice) {\n    await confirmGitHubRepository();" in script
    assert "body: JSON.stringify({ option_id: choice.selected })" in script
    assert ".integration-project-empty" in stylesheet
    assert ".integration-setup-prompt" in stylesheet
    assert "Gmail read + send · Calendar read" in script
    assert "Enable sending" in script
    assert "incremental send permission" not in script
    assert ">Connect</button>" in script
    assert 'selection?.addEventListener("change"' in script
    assert "Choose a ${optionLabel.toLowerCase()}" in script
    assert 'connected ? "Configure" : "Details"' not in script
    assert '"analytics.gsc": "/assets/integrations/google-search-console.svg"' in script
    assert '"infra.github": "/assets/integrations/github.svg"' in script
    assert '"workspace.google": "/assets/integrations/google-workspace.svg"' in script
    assert "write access for only the repositories granted in GitHub" in script
    assert 'url.pathname.startsWith("/integrations/callback/")' in script
    assert "bindTinControls(form)" in script
    assert ".workflow-config-form:not(.workflow-config-ledger)" in script
    assert "projectWorkflowLedger(workflow, configured, editor.field)" in script
    assert 'data-edit-workflow-field="${escapeHtml(field)}"' in script
    assert 'data-workflow-field="schedule"' in script
    assert "expected_settings_revision: configured.settings_revision" in script
    assert (
        "settings updated ${escapeHtml(timeLabel(configured.updated_at))} · edits logged" in script
    )
    assert "config v" not in script
    assert "<select" not in script
    assert ".tin-select-menu" in stylesheet
    assert "box-shadow: var(--shadow-menu)" in stylesheet
    assert ".tin-select-option.is-selected .tin-select-marker" in stylesheet
    assert ".workflow-ledger-row.is-editing" in stylesheet
    assert ".workflow-ledger-footer" in stylesheet
    assert ".workflow-ledger-form.is-invalid .workflow-field-message" in stylesheet
    assert "box-shadow: inset 0 -2px 0 var(--accent-deep)" not in stylesheet
    assert ".workflow-config-row" in stylesheet
    assert 'script.src = "/assets/diagram-renderer.js"' in diagram_loader
    assert "window.TinDiagramLoader.load()" in script
    assert "derived from the pinned definition" in script
    assert "data-workflow-diagram" in script
    assert "data-project-diagram" in script
    assert "TinDiagramRenderer" in diagram_script
    assert "var(--diagram-edge)" in diagram_script
    assert (
        ".integration-list {\n  display: flex;\n  flex-direction: column;\n  gap: 8px" in stylesheet
    )
    assert ".integration-card {\n  background: transparent;\n  border: 1px solid" in stylesheet
    assert ".integration-card-row {\n  display: flex;" in stylesheet
    assert ".integration-connect" in stylesheet
    assert ".integration-card {\n  background: transparent;\n  border-bottom" not in stylesheet
    assert "workflowEditor(workflow) || registryWorkflowCard(workflow, query)" in script
    assert "data-run-intent" in script
    assert 'const configureLabel = canRunWithDefaults ? "Configure" : "Set up";' in script
    assert "data-run-workflow-draft" in script
    assert "Run now uses these inputs once · saving pins v" in script
    assert 'return "Latest report"' in script
    assert 'return "Latest draft"' in script
    assert "body: JSON.stringify({ project_id: context.projectId, inputs })" in script
    assert "/workflows/${encodeURIComponent(projectWorkflowId)}/runs" in script
    assert 'gap: "18px",\n      overflow: "visible"' in script
    assert 'logoBox: { display: "none" }' in script
    assert 'socialButtonsRoot: { marginTop: 0, overflow: "visible" }' in script
    assert "lastAuthenticationStrategyBadge" in script
    assert 'insetInlineEnd: "10px"' in script
    assert 'footerActionLink: {\n      color: "var(--hot)"' in script
    assert (
        'colorScheme: document.documentElement.dataset.theme === "dark" ? "dark" : "light"'
        in script
    )
    assert "function tinFilesTheme()" in script
    assert "var(--file-icon-surface)" in script
    assert 'const STORAGE_KEY = "tin-lite:theme"' in theme_script
    assert 'new Set(["system", "light", "dark"])' in theme_script
    assert 'window.matchMedia("(prefers-color-scheme: dark)")' in theme_script
    assert 'window.dispatchEvent(new CustomEvent("tin:themechange"' in theme_script
    assert "window.TinTheme = { apply, storedPreference, syncControls }" in theme_script
    assert 'data-theme-preference="system"' in index
    assert 'data-theme-preference="light"' in index
    assert 'data-theme-preference="dark"' in index
    assert 'aria-label="Appearance"' in index
    assert 'window.addEventListener("tin:themechange"' in script
    assert "disposeFilesTree();\n    renderFiles();" in script
    assert 'justifyContent: "flex-start",\n      background: "transparent"' in script
    assert 'otpCodeField: { alignSelf: "flex-start" }' in script
    assert 'otpCodeFieldInputs: { gap: "8px", justifyContent: "flex-start" }' in script
    assert ".cl-card:has(.cl-otpCodeField) .cl-form {" in stylesheet
    assert ".cl-main .cl-footerAction {" in stylesheet
    assert 'margin: "44px 0 16px"' in script
    assert '"> :last-child": { padding: "16px 0 0" }' in script
    assert '"& > div > div": { justifyContent: "flex-start" }' in script
    assert 'api("/api/projects")' in script
    assert "api(`/api/projects/${projectId}/chat/messages?limit=100`)" in script
    assert "window.crypto.randomUUID()" in script
    assert "request_id: requestId" in script
    assert script.count('chatDraft: ""') == 1
    assert "chatDrafts: new Map()" in script
    assert "if (!thread || !form || !field || !sendButton)" in script
    assert "state.chatDraft = field.value;" in script
    assert "if (field.value !== state.chatDraft)" in script
    assert 'state.chatDraft = "";' in script
    assert "state.chatDrafts.set(state.project.id, field.value)" in script
    assert 'aria-haspopup="dialog"' in index
    assert 'id="project-menu"' in index
    assert 'width="0" height="0" style="position:absolute;overflow:hidden"' in script
    assert ".rail {\n  position: sticky;\n  top: 0;\n  z-index: 10;" in stylesheet
    assert 'item.setAttribute("aria-current", "true")' in script
    assert "top: 0;\n  left: calc(100% + 27px);\n  width: 360px;" in stylesheet
    assert "position: fixed;\n    top: var(--top-menu-height);\n    left: 18px;" in stylesheet
    assert ".project-menu-item.is-current {\n  background: rgb(var(--ink-rgb) / 6%);" in stylesheet
    assert "project.member_count" in script
    assert "Invite someone →" in script
    assert 'id="project-invite-dialog"' in index
    assert "/invitations`" in script
    assert "function selectedProject(projects, invitedProjectId = null)" in script
    assert 'url.searchParams.set("project", projectId)' in script
    assert "tin-lite:project:${state.signedInUserId}" in script
    assert "function isCurrentProjectContext(context)" in script
    assert 'id="project-delete-dialog"' in index
    assert "Delete project →" in script
    assert "state.project?.can_delete" in script
    assert "function openProjectDelete()" in script
    assert 'id="project-delete-prompt"' in index
    assert "projectDeleteName.placeholder = state.project.name" in script
    assert "function afterProjectDeleted(deleted)" in script
    assert "function forgetProjectSelection(projectId)" in script
    assert "confirm_name: name" in script
    assert "Billing history stays." in script
    assert ".project-delete-dialog .button.is-danger" in stylesheet
    assert "can_delete: bool = False" in api_source
    assert '@router.delete("/api/projects/{project_id}"' in api_source
    assert 'data-view="decisions"' in index
    assert 'id="agent-rail"' in index
    assert 'class="eyebrow"' not in index
    assert "Claude Code · Codex · API" in index
    assert "Paste one line into your terminal" in index
    # One install line per agent, then the prompt as a comment; the sign-in opens by itself.
    assert 'const AGENT_PROMPT = "Use Tin to grow my project like a pro!";' in script
    assert "`codex mcp add tin --url ${mcpUrl}`" in script
    assert "`claude mcp add -t http tin ${mcpUrl}`" in script
    assert "codex mcp login" not in script
    assert "Copied. Run it, then ask your coding agent" in script
    # One-page connect flow from the agent's link, and the first-run line under System.
    assert 'if (url.pathname.replace(/\\/$/, "") !== "/connect") return;' in script
    assert "function renderConnectRequest(requested)" in script
    assert "go back to your agent and say: connected." in script
    assert 'if (view !== "integrations") clearConnectRequest();' in script
    assert "Set up today: ${workflowCountValue}" in script
    assert "copyAgentCommand.flipTimer = window.setTimeout(() => updateAgentRail(), 4000)" in script
    assert "27 tools · workflows, runs, files, integrations" in index
    assert "function renderDecisions()" in script
    assert "function decisionsPace()" in script
    assert "nearest deadline ${systemDateTime(nearestDeadline)}" in script
    assert "${escapeHtml(item.title)}</strong></span>" in script
    assert "${escapeHtml(item.workflow_title)}</strong><small>" not in script
    assert "${escapeHtml(decision.title)}</h2>" not in script
    assert 'data-decision-read="${escapeHtml(decision.id)}">Observe →' in script
    assert 'const showRunAction = decision.kind === "output_conflict" || !outputs.length;' in script
    assert '${showRunAction ? `<button type="button" data-decision-read=' in script
    assert 'const consequence = String(decision.consequence || "").trim();' in script
    assert 'class="is-actions-only"' in script
    assert ".decision-detail-card > footer.is-actions-only" in stylesheet
    assert "function systemAddWorkflowsHtml(" in script
    template_card_source = script[
        script.index("function systemTemplateCard(") : script.index(
            "function systemTemplateSetupCard("
        )
    ]
    assert "system-card-dot" not in template_card_source
    assert '<span class="system-template-description">' in template_card_source
    assert "font-family: var(--sans);" in stylesheet
    assert "function updateAgentRail()" in script
    assert '"not connected yet"' in script
    assert "Tin could not load this project." in script
    assert "Tin could not load ${escapeHtml(project.name)}" not in script
    assert "api(`/api/projects/${encodeURIComponent(projectId)}/runs?limit=100`)" in script
    assert "immediate ? 0 : active ? 1800 : 5000" in script
    assert "updateWorkflowRunRegions()" in script
    assert 'document.addEventListener("visibilitychange"' in script
    assert "@media (max-width: 900px)" in stylesheet
    assert "--top-menu-height: 105px" in stylesheet
    assert 'api("/api/workspaces/bootstrap", {' in script
    assert "Preparing your first project…" in script
    assert "function personalProjectName()" in script
    assert "function openProjectCreate(" in script
    assert "can_create_project_in_workspace" in script
    assert script.index("await acceptPendingInvitation()") < script.index(
        "await bootstrap(invitedProject?.id || integrationReturn?.projectId || null, "
        "integrationReturn)"
    )
    assert 'projectAccess: "loading"' in script
    assert "if (!hasProject)" in script
    assert ".nav-item:disabled" in stylesheet
    # Browser sign-ups: a project without a workflow or run is locked behind the coding-agent page.
    assert 'data-browser-lock-enabled="{{BROWSER_LOCK_ENABLED}}"' in index
    assert (
        '"{{BROWSER_LOCK_ENABLED}}": str(getattr(settings, "browser_lock_enabled", True)).lower()'
        in api_source
    )
    assert (
        "state.projectAccess = BROWSER_LOCK_ENABLED && !projectWorkflows.length && !runs.length"
        in script
    )
    assert '!runs.length ? "locked" : "ready"' in script
    # Lock routing, including the agent connection exception, is exercised in Chromium
    # by web/lock-page.browser.test.js rather than matching one rendering branch here.
    assert "function renderLockPage()" in script
    assert 'agentRail.hidden = state.projectAccess === "locked"' in script
    assert "Set up Tin from your coding agent" in script
    assert "Browser setup is not available yet." in script
    assert 'api("/api/events/lock-page", {' in script
    assert ".lock-page-line" in stylesheet
    # State construction calls viewFromLocation(), which reads this constant synchronously.
    assert script.index("const ALLOWED_VIEWS") < script.index("const state")
    assert index.index('data-view="workflows"') < index.index('data-view="chat"')
    assert 'class="nav-item is-active" type="button" data-view="workflows"' in index
    assert 'aria-label="Tin is typing"' in script
    assert "state.sending ? renderTypingTurn()" in script
    assert ".turn-typing > span:nth-child(3)" in stylesheet
    assert ".button:not(:disabled):hover" in stylesheet
    assert ".send-button:not(:disabled):hover" in stylesheet
    assert ".workspace-view,\n.activity-view,\n.product-view" in stylesheet
    assert 'class="product-view workspace-view system-view"' in script
    assert 'class="product-view decisions-view"' in script
    assert 'class="product-view files-view"' in script
    assert 'class="product-view integrations-view"' in script
    assert "live · tailing runs" in script
    assert 'activityFilterButton("needs_you", "Needs you")' in script
    assert 'activityFilterButton("your_edits", "Your edits")' in script
    assert "groupActivityByDay(events)" in script
    assert "data-activity-artifact" in script
    assert 'class="needs-you-queue"' in script
    assert 'data-review-run="${escapeHtml(head.id)}"' in script
    assert 'data-defer-review="${escapeHtml(head.id)}"' in script
    assert 'data-approve-run="${escapeHtml(run.id)}"' not in script
    assert "pendingReviewQueue()" in script
    assert "queue.slice(1, 4)" in script
    assert "queue.length - 4" in script
    assert "/approve`" in script
    assert 'isCampaignRevisionReview(run) ? "Approve revision" : "Approve draft"' in script
    assert 'label: "Discard revision"' in script
    assert ".needs-you-primary" in stylesheet
    assert "background: var(--hot-wash)" in stylesheet
    assert "grid-template-columns: 7px 210px" in stylesheet
    assert ".status-dot.is-needs_input" in stylesheet
    assert "scroll for earlier activity" in script
    assert "grid-template-columns: 44px 14px 195px" in stylesheet
    assert ".activity-marker.is-founder" in stylesheet
    assert ".activity-marker.is-failed" in stylesheet
    assert ".activity-ledger-row:hover" in stylesheet
    assert "Postgres live" not in script
    assert "activity-item" not in stylesheet
    assert '<header class="chat-header">' not in script
    assert '<div class="chat-suggestions"' not in script
    assert "<h1>What should we move forward?</h1>" in script
    assert "Your workflows" in script
    assert "Registry" in script
    assert "function activeUnsavedWorkflowRuns()" in script
    assert "function activeRunsByWorkflow()" not in script
    assert "!run.project_workflow_id &&" in script
    assert 'run.workflow_name !== "project.task"' in script
    assert "<strong>Live runs</strong>" in script
    assert "data-live-run-activity" not in script
    assert "activeRuns.filter" not in script
    assert "cards.push(...runs.map((run) => systemRunCard(run, configured)))" in script
    assert ".live-workflow-runs" in stylesheet
    assert ".live-workflow-run" in stylesheet
    assert "padding: 0 16px 14px 44px" in stylesheet
    assert "padding: 0 14px 14px 40px" in stylesheet
    assert ".system-run-detail" in stylesheet
    assert ".campaign-delivery-row" in stylesheet
    assert 'class="status-dot is-active"' in script
    assert 'const UNASSIGNED_WORKFLOW_SYSTEM = "__unassigned__";' in script
    assert "function registrySystemGroups(workflows)" in script
    assert "function registrySystemFact(group, total, searching)" in script
    assert 'function registryWorkflowCard(workflow, query = "")' in script
    assert 'name: unassigned ? "Not in a system" : workflow.system_name' in script
    assert "Search looks at workflow names and ids across all" in script
    assert "data-ask-luna-workflow" in script
    assert ".registry-system-header" in stylesheet
    assert ".registry-system-workflows" in stylesheet
    assert ".registry-search-empty" in stylesheet
    assert ".workflow-search-title-match" in stylesheet
    assert ".workflow-search-id-match" in stylesheet
    assert "function workflowSearchProjection(" in script
    assert "function updateWorkflowSearchResults()" in script
    assert 'id="workflow-run-regions"' in script
    assert 'id="workflow-results"' in script
    assert (
        "state.workflowSearch = event.target.value;\n    updateWorkflowSearchResults();" in script
    )
    assert (
        'renderWorkflows();\n    const search = document.querySelector("#workflow-search");'
        not in script
    )
    assert "scrollbar-gutter: stable" in stylesheet
    assert ".search-field.is-registry input {\n  padding-right: 33px;" in stylesheet
    assert "activeByWorkflow.get(workflow.id)" not in script
    assert '<button class="button" type="button" disabled>Running</button>' not in script
    assert "review waiting" not in script
    assert 'mcp: "via MCP"' in script
    assert 'schedule: "by schedule"' in script
    assert 'manual: "via dashboard"' in script
    assert "from Registry" not in script
    assert "const waiting = state.runs.filter" in script
    assert "const byWorkflow = new Map()" not in script
    assert ".chat-view {\n  display: flex;\n  height: 100svh;" in stylesheet
    assert ".chat-sheet {\n  display: flex;" in stylesheet
    assert "height: calc(100svh - 22px);\n  min-height: 0;" in stylesheet
    assert ".chat-thread > :first-child {\n  margin-top: auto;" in stylesheet
    chat_thread_styles = stylesheet.split(".chat-thread {", 1)[1].split("}", 1)[0]
    assert "scrollbar-width: none;" in chat_thread_styles
    assert ".chat-thread::-webkit-scrollbar {\n  display: none;" in stylesheet
    assert "justify-content: flex-end" not in chat_thread_styles
    assert ".chat-composer {\n  display: flex;\n  flex: 0 0 auto;" in stylesheet
    assert "width: fit-content" in stylesheet
    assert "window.TinMarkdownViewer = { mount }" in viewer_script
    assert 'if (options.mode !== "in-app")' in viewer_script
    assert "if (options.primaryAction)" in viewer_script
    assert "options.primaryAction.onActivate(action)" in viewer_script
    assert "if (options.secondaryAction)" in viewer_script
    assert 'logoImage.src = "/assets/tin-logotype-ink.png"' in viewer_script
    assert ".markdown-context-bar {\n  position: sticky" in stylesheet
    assert "markdown-reader-layout" in stylesheet
    assert "`/api/workflows/runs/${encodeURIComponent(route.runId)}/artifact/document`" in script
    assert "`/api/tasks/${encodeURIComponent(route.runId)}/review/document?path=" in script
    assert 'data-view="files"' in index
    assert "/assets/pierre-trees.js?v={{ASSET_VERSION}}" in index
    assert "/assets/delimited-viewer.js?v={{ASSET_VERSION}}" in index
    assert "window.TinFilesTree={FileTree:" in trees_script
    assert "prepareFileTreeInput(paths" in script
    assert "flattenEmptyDirectories: true" in script
    assert "initialExpansion: 1" in script
    assert "itemHeight: 40" in script
    assert "Search project files…" in script
    assert "Workflows and Codex runs begin from this shared state." in script
    assert "Proposed files stay on the task page until you approve them." in script
    assert (
        "No project files yet. Completed workflows and approved task changes will appear here."
        in script
    )
    assert "This file changed with the project. Open the latest version." in script
    assert "Tin could not open this file." in script
    assert "window.TinDelimitedViewer.readResponse(response)" in script
    assert "window.TinDelimitedViewer" in delimited_script
    assert "Tin couldn't read this as a table · showing the file as text" in delimited_script
    assert "first column pinned · header sticks while you scroll" in delimited_script
    assert ".project-delimited-table" in stylesheet
    assert "data-files-directory" in script
    assert "@media (max-width: 720px)" in stylesheet
    assert ".files-mobile-drill" in stylesheet
    assert "gitStatus:" not in script


def test_run_outputs_of_every_media_kind_open_in_the_product_viewer() -> None:
    from tin_lite.api import _file_media_type

    static = Path(__file__).resolve().parents[1] / "src" / "tin_lite" / "static"
    script = (static / "app.js").read_text()
    stylesheet = (static / "app.css").read_text()

    # The raw artifact endpoint labels every studio format so the viewer can pick a renderer.
    assert _file_media_type("studio/character.svg") == "image/svg+xml"
    assert _file_media_type("studio/demo.MP4") == "video/mp4"
    assert _file_media_type("studio/voice.m4a") == "audio/mp4"
    assert _file_media_type("studio/brief.pdf") == "application/pdf"
    assert _file_media_type("diagrams/flow.mmd") == "text/vnd.mermaid"
    assert _file_media_type("reports/SCAN.md") == "text/markdown"
    assert _file_media_type("unknown.blob") == "application/octet-stream"

    # Non-Markdown outputs route to the authenticated in-app file viewer, never to a raw
    # link the session header cannot follow, and the viewer knows the broad media set.
    assert "function openRunOutputFile(run, output, returnView" in script
    assert 'target="_blank" rel="noreferrer">Open →</a>' not in script
    assert '/artifact" target="_blank"' not in script
    media_table = script.split("const MEDIA_FILE_TYPES = {", 1)[1].split("};", 1)[0]
    for extension in ("svg", "png", "gif", "webp", "mp4", "webm", "mov", "mp3", "wav", "flac"):
        assert f"{extension}: " in media_table
    assert "pdf: " in media_table
    assert '<iframe class="project-media-pdf"' in script
    assert ".project-media-pdf {" in stylesheet


@pytest.mark.asyncio
async def test_diagram_routing_asset_is_packaged_and_served_as_wasm() -> None:
    from fastapi.staticfiles import StaticFiles

    from tin_lite.api import STATIC_DIR

    app = FastAPI()
    app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/assets/diagram-routing.wasm")
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/wasm"
    assert response.content.startswith(b"\x00asm")


async def test_rendered_shell_assets_are_served_by_fastapi():
    from fastapi.staticfiles import StaticFiles

    from tin_lite.api import STATIC_DIR

    app = FastAPI()
    app.include_router(router)
    app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")
    app.state.settings = SimpleNamespace(
        clerk_publishable_key="pk_test_fixture",
        clerk_frontend_api_url="https://clerk.test",
        switchboard_public_url="http://test",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        landing = await client.get("/")
        assert 'data-browser-lock-enabled="true"' in landing.text
        paths = set(re.findall(r'(?:src|href)="(/assets/[^\"]+)"', landing.text))
        assert any("code-workflow-setup.js" in path for path in paths)
        assert any("code-workflow-setup.css" in path for path in paths)
        for path in paths:
            response = await client.get(path)
            assert response.status_code == 200 and response.content, path


def test_approval_offers_pull_request_or_publish_now_when_github_is_connected() -> None:
    """Emre's picks (Paper board P2-m: A + E + G): the reviewer chooses the delivery."""
    static = Path(__file__).parents[1] / "src" / "tin_lite" / "static"
    script = (static / "app.js").read_text()
    review_script = (static / "workflow-review.js").read_text()
    stylesheet = (static / "app.css").read_text()
    api_source = (static.parent / "api.py").read_text()
    # API: approve and the decision apply both accept the pick and the remember flag.
    assert (
        api_source.count('delivery: Literal["github_pr", "github_commit", "none"] | None = None')
        == 2
    )
    assert api_source.count("remember: bool = False") == 2
    assert "await _choose_content_delivery(run, payload, request, user)" in api_source
    assert "delivery=payload.delivery,\n                remember=payload.remember," in api_source
    # Decisions card: two choices plus Not now and the remember checkbox when GitHub is connected.
    assert (
        'new Set(["content.generate", "content.public_article", "content.answer_page"])' in script
    )
    assert "function connectedRepository()" in script
    assert "function repositoryDeliveryAvailable(run)" in script
    assert 'data-delivery="github_commit">Publish now</button>' in script
    assert 'data-delivery="github_pr">Open a pull request</button>' in script
    assert (
        '<label class="decision-remember"><input type="checkbox" data-decision-remember>' in script
    )
    assert "data-decision-remember> Do this for future drafts</label>" in script
    assert "data-decision-not-now>Not now</button>" in script
    assert "Approved drafts stay in Tin until GitHub is connected." in script
    assert 'href="/integrations" data-decision-connect-github>Connect GitHub</a>' in script
    assert "const label = run?.content_delivery?.approval_label ||" in script
    assert '? "Use documents" : "Approve"' in script
    assert "!run?.content_delivery?.system_run_id && isContentDraftReview(run)" in script
    assert 'main.querySelectorAll("[data-apply-decision]").forEach' in script
    assert "const delivery = button.dataset.delivery || null;" in script
    assert 'main.querySelector("[data-decision-remember]")?.checked' in script
    assert "...(delivery ? { delivery, remember } : {})," in script
    # Reader top bar: Request changes, Open a pull request, Publish now (primary).
    assert (
        "async function approveRun(runId, button, { delivery = null, remember = false } = {})"
        in script
    )
    assert (
        'repositoryDeliveryAvailable(run) ? "Publish now" : run?.content_delivery?.approval_label'
        in script
    )
    assert 'repositoryDeliveryAvailable(run) ? { delivery: "github_commit" } : {})' in script
    assert (
        'deliveryOptions: reader && repositoryDelivery ? [{ label: "Open a pull request"' in script
    )
    assert '[{ label: "Open a pull request", delivery: "github_pr" }] : []' in script
    assert "context.deliveryOptions || []" in review_script
    assert "is-delivery-option" in review_script
    assert "context.onApprove(button, option.delivery)" in review_script
    assert 'review.artifact?.assessment ? "Give feedback" : "Request changes"' in review_script
    assert 'context.approvalLabel || "Approve draft"' in review_script
    assert "Draft approved. Publishing it to the repository now." in script
    assert ".decision-detail-card > footer .decision-remember" in stylesheet
