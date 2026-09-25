from __future__ import annotations

import hashlib
import logging
import mimetypes
import re
import secrets
from datetime import UTC, datetime, time, timedelta
from html import escape
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator

from tin_lite import analytics, project_task_control
from tin_lite.auth import AuthContext, require_user
from tin_lite.billing_contracts import BillingError
from tin_lite.campaign_revisions import request_email_campaign_revision
from tin_lite.codex_api_relay import router as codex_api_router
from tin_lite.content_delivery_api import router as content_delivery_router
from tin_lite.content_draft_api import router as content_draft_router
from tin_lite.content_program_api import router as content_program_router
from tin_lite.documents import render_markdown
from tin_lite.domain import (
    EMAIL_CAMPAIGN_WORKFLOW_NAME,
    PROJECT_TASK_WORKFLOW_NAME,
    WORKFLOW_NAME,
    ChatMessage,
    IntegrationConnection,
    Project,
    ProjectWorkflow,
    RunStatus,
    SideEffectConflictError,
    StaleSettingsRevisionError,
    Workflow,
    WorkflowRun,
    WorkflowStatus,
    Workspace,
)
from tin_lite.growth_onboarding import KEY as GROWTH_ONBOARDING_KEY
from tin_lite.growth_onboarding_control import OnboardingPickError, ensure_onboarding_approvable
from tin_lite.integrations import (
    ADS_PROVIDER,
    GITHUB_PROVIDER,
    GOOGLE_WORKSPACE_PROVIDER,
    GSC_PROVIDER,
    POSTHOG_PROVIDER,
    STRIPE_PROVIDER,
    GitHubInstallationChoiceError,
    GitHubInstallationRequiredError,
    IntegrationAuthorizationError,
    IntegrationDefinition,
    IntegrationError,
    IntegrationInputError,
    IntegrationNotConfiguredError,
    IntegrationUpstreamError,
    ServiceCallRefused,
    registered_integrations,
)
from tin_lite.keyword_plan_control import stop_keyword_plan as stop_keyword_plan_service
from tin_lite.luna import LunaProtocolError, LunaSafetyError, LunaUpstreamError
from tin_lite.organic_audit_control import stop_organic_audit as stop_organic_audit_service
from tin_lite.output_resolution import OutputResolutionError, OutputResolutionRequest
from tin_lite.paid_ads_control import (
    stop_paid_ads_assessment as stop_paid_ads_assessment_service,
)
from tin_lite.paid_ads_control import stop_paid_ads_launch as stop_paid_ads_launch_service
from tin_lite.paid_ads_control import stop_paid_ads_monitor as stop_paid_ads_monitor_service
from tin_lite.paid_ads_proposals import (
    approve_paid_ads_proposal as approve_paid_ads_proposal_service,
)
from tin_lite.paid_ads_proposals import (
    discard_paid_ads_proposal as discard_paid_ads_proposal_service,
)
from tin_lite.paid_ads_proposals import list_paid_ads_proposals as list_paid_ads_proposals_service
from tin_lite.private_workflow_api import router as private_workflow_router
from tin_lite.private_workflows import private_execution_ready, workflow_source_view
from tin_lite.product_urls import dashboard_url
from tin_lite.project_connections_api import router as project_connections_router
from tin_lite.project_deletion import ProjectDeletionPending, deletable_project
from tin_lite.project_deletion import delete_project as delete_project_service
from tin_lite.project_files import (
    ProjectFileMutationInput,
    StaleProjectRevisionError,
    safe_project_file_path,
)
from tin_lite.projects import (
    ProjectCreationConflictError,
    ProjectProvisioningError,
    can_delete_project,
    normalize_project_name,
    provision_personal_project,
    provision_workspace_project,
)
from tin_lite.publication import RunOutput, read_run_output
from tin_lite.run_service import (
    TemporalStartError,
    WorkflowExecutorUnavailableError,
)
from tin_lite.run_service import (
    start_workflow_run as dispatch_workflow_run,
)
from tin_lite.schedules import WorkflowSchedule, next_run_after
from tin_lite.technical_fix_api import router as technical_fix_router
from tin_lite.technical_fix_api import system_router as organic_system_router
from tin_lite.workflow_inputs import client_input_schema, normalize_workflow_inputs
from tin_lite.workflow_prerequisites import PrerequisiteError, project_readiness


class _RunSafeRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def safe_handler(request: Request):
            try:
                return await handler(request)
            except RequestValidationError:
                if request.method == "POST" and request.url.path.endswith("/runs"):
                    # Pydantic's default diagnostics can echo the entire request body.
                    raise HTTPException(status_code=422, detail="Invalid run request.") from None
                raise

        return safe_handler


router = APIRouter(route_class=_RunSafeRoute)

router.include_router(codex_api_router)
router.include_router(private_workflow_router)
router.include_router(content_program_router)
router.include_router(content_draft_router)
router.include_router(content_delivery_router)
router.include_router(technical_fix_router)
router.include_router(organic_system_router)
router.include_router(project_connections_router)
logger = logging.getLogger(__name__)
AUTHENTICATED_USER = Depends(require_user)


@router.post("/api/events/lock-page", status_code=204)
async def record_lock_page_event(request: Request, user: AuthContext = AUTHENTICATED_USER):
    """A browser sign-up met the locked dashboard: opened it, or copied the install line.

    The lock stands in for browser onboarding until it exists; these two events show how
    many people it sends to their coding agent. The body is a fixed vocabulary, never
    free text.
    """
    body = await request.json() if await request.body() else {}
    body = body if isinstance(body, dict) else {}
    action = body.get("action")
    if action not in {"viewed", "install_copied"}:
        raise HTTPException(status_code=400, detail="Unknown lock page action.")
    agent = body.get("agent")
    project_id = body.get("project_id")
    analytics.capture(
        f"lock_page_{action}",
        distinct_id=user.clerk_user_id,
        properties={
            "clerk_user_id": user.clerk_user_id,
            "agent": agent if agent in {"codex", "claude", "api"} else None,
        },
        project_id=str(project_id) if isinstance(project_id, str) and project_id else None,
    )
    return Response(status_code=204)


BILLING_QUOTE_HEADER = Header(default=None, alias="Tin-Billing-Quote")


@router.get("/api/projects/{project_id}/writing-style/guide")
async def get_writing_style_guide(
    project_id: UUID, request: Request, user: AuthContext = AUTHENTICATED_USER
):
    from tin_lite.writing_style import writing_style_guide

    if not await request.app.state.runtime.database.has_project_access(
        project_id=project_id, clerk_user_id=user.clerk_user_id
    ):
        raise HTTPException(status_code=404, detail="project not found")
    return JSONResponse(writing_style_guide(), headers={"Cache-Control": "no-store"})


@router.post("/api/projects/{project_id}/writing-style/preview")
async def preview_writing_sample(
    project_id: UUID,
    request: Request,
    filename: str = Query(max_length=200),
    user: AuthContext = AUTHENTICATED_USER,
):
    from tin_lite.style_samples import MAX_UPLOAD_BYTES, extract_sample

    await _require_project_access(project_id, request, user)
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="Choose a file no larger than 5 MiB.")
    try:
        sample = extract_sample(filename, bytes(content))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return JSONResponse(sample, headers={"Cache-Control": "no-store"})


@router.post("/api/workflows/runs/{run_id}/stop-procedure")
async def stop_codex_procedure_run(
    run_id: UUID, request: Request, user: AuthContext = AUTHENTICATED_USER
) -> dict:
    from tin_lite.procedure_control import stop_procedure

    try:
        return await stop_procedure(
            runtime=request.app.state.runtime, run_id=run_id, actor=user.clerk_user_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    except SideEffectConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/workflows/runs/{run_id}/stop-organic-audit")
async def stop_organic_audit_run(
    run_id: UUID, request: Request, user: AuthContext = AUTHENTICATED_USER
) -> dict:
    try:
        run = await stop_organic_audit_service(
            runtime=request.app.state.runtime, run_id=run_id, clerk_user_id=user.clerk_user_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    except (ValueError, SideEffectConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": str(run.id), "status": run.status.value}


@router.post("/api/workflows/runs/{run_id}/stop-paid-ads-assessment")
async def stop_paid_ads_assessment_run(
    run_id: UUID, request: Request, user: AuthContext = AUTHENTICATED_USER
) -> dict:
    try:
        run = await stop_paid_ads_assessment_service(
            runtime=request.app.state.runtime, run_id=run_id, clerk_user_id=user.clerk_user_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    except (ValueError, SideEffectConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": str(run.id), "status": run.status.value}


@router.post("/api/workflows/runs/{run_id}/stop-paid-ads-launch")
async def stop_paid_ads_launch_run(
    run_id: UUID, request: Request, user: AuthContext = AUTHENTICATED_USER
) -> dict:
    try:
        run = await stop_paid_ads_launch_service(
            runtime=request.app.state.runtime, run_id=run_id, clerk_user_id=user.clerk_user_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    except (ValueError, SideEffectConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": str(run.id), "status": run.status.value}


@router.post("/api/workflows/runs/{run_id}/stop-paid-ads-monitor")
async def stop_paid_ads_monitor_run(
    run_id: UUID, request: Request, user: AuthContext = AUTHENTICATED_USER
) -> dict:
    try:
        run = await stop_paid_ads_monitor_service(
            runtime=request.app.state.runtime, run_id=run_id, clerk_user_id=user.clerk_user_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    except (ValueError, SideEffectConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": str(run.id), "status": run.status.value}


@router.get("/api/projects/{project_id}/paid-ads/proposals")
async def list_paid_ads_proposals_route(
    project_id: UUID, request: Request, user: AuthContext = AUTHENTICATED_USER
) -> list[dict]:
    try:
        rows = await list_paid_ads_proposals_service(
            runtime=request.app.state.runtime,
            project_id=project_id,
            clerk_user_id=user.clerk_user_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="project not found") from exc
    return [_proposal_view(row) for row in rows]


@router.post("/api/paid-ads/proposals/{proposal_id}/approve")
async def approve_paid_ads_proposal_route(
    proposal_id: UUID, request: Request, user: AuthContext = AUTHENTICATED_USER
) -> dict:
    try:
        row = await approve_paid_ads_proposal_service(
            runtime=request.app.state.runtime,
            proposal_id=proposal_id,
            clerk_user_id=user.clerk_user_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="proposal not found") from exc
    except (RuntimeError, ValueError, SideEffectConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _proposal_view(row)


@router.post("/api/paid-ads/proposals/{proposal_id}/discard")
async def discard_paid_ads_proposal_route(
    proposal_id: UUID, request: Request, user: AuthContext = AUTHENTICATED_USER
) -> dict:
    try:
        row = await discard_paid_ads_proposal_service(
            runtime=request.app.state.runtime,
            proposal_id=proposal_id,
            clerk_user_id=user.clerk_user_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="proposal not found") from exc
    except (RuntimeError, ValueError, SideEffectConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _proposal_view(row)


def _proposal_view(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "campaign_run_id": str(row["campaign_run_id"]),
        "monitor_run_id": str(row["monitor_run_id"]),
        "number": row["proposal_number"],
        "kind": row["kind"],
        "status": row["status"],
        "previous": row["previous"],
        "proposed": row["proposed"],
        "rationale": row["rationale"],
        "review_path": row["review_path"],
        "review_commit_sha": row.get("review_commit_sha"),
        "requested_at": row["requested_at"].isoformat() if row.get("requested_at") else None,
        "reviewed_at": row["reviewed_at"].isoformat() if row.get("reviewed_at") else None,
        "error_code": row.get("error_code"),
    }


@router.post("/api/workflows/runs/{run_id}/stop-keyword-plan")
async def stop_keyword_plan_run(
    run_id: UUID, request: Request, user: AuthContext = AUTHENTICATED_USER
) -> dict:
    try:
        run = await stop_keyword_plan_service(
            runtime=request.app.state.runtime, run_id=run_id, clerk_user_id=user.clerk_user_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="run not found") from exc
    except (ValueError, SideEffectConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"id": str(run.id), "status": run.status.value}


STATIC_DIR = Path(__file__).with_name("static")
ASSET_VERSION = hashlib.sha256(
    b"".join(
        (STATIC_DIR / filename).read_bytes()
        for filename in (
            "app.css",
            "app.js",
            "auth-appearance.js",
            "auth.css",
            "mcp-consent.js",
            "mcp-consent.css",
            "billing.css",
            "billing.js",
            "content-plan.js",
            "content-delivery.js",
            "style-capture.js",
            "content-draft.js",
            "delimited-viewer.js",
            "json-viewer.js",
            "diagram-loader.js",
            "diagram-renderer.js",
            "diagram-routing.wasm",
            "markdown-viewer.js",
            "output-comparison-page.js",
            "output-comparison.css",
            "output-comparison.js",
            "pierre-trees.js",
            "theme.js",
            "tin-favicon.svg",
            "viewer-page.js",
        )
    )
).hexdigest()[:12]


def _static_page(filename: str, request: Request) -> Response:
    from tin_lite.fonts import private_font_stylesheet
    from tin_lite.product_urls import product_origins

    settings = request.app.state.settings
    app_url = dashboard_url(settings)
    service_url = str(settings.switchboard_public_url).rstrip("/")
    if (
        str(request.base_url).rstrip("/") != app_url
        and str(request.base_url).rstrip("/") in product_origins(settings)
        and request.method in {"GET", "HEAD"}
        and (
            request.url.path
            in {
                "/",
                "/connect",
                "/system",
                "/workflows",
                "/chat",
                "/activity",
                "/decisions",
                "/files",
                "/integrations",
                "/billing",
                "/file",
            }
            or request.url.path.startswith(("/document/", "/task/", "/compare/"))
            or filename == "viewer.html"
        )
        and "redirect_url" not in request.query_params
    ):
        # Browser fragments are inherited because Location has no fragment. Keep
        # OAuth, callbacks, APIs and MCP on their original protocol endpoints.
        target = f"{app_url}{request.url.path}"
        if request.url.query:
            target += f"?{request.url.query}"
        return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store"})
    html = (STATIC_DIR / filename).read_text()
    replacements = {
        "{{ASSET_VERSION}}": ASSET_VERSION,
        "<!--PRIVATE_FONTS_STYLESHEET-->": private_font_stylesheet(settings),
        "{{BILLING_ENABLED}}": str(getattr(settings, "billing_enabled", False)).lower(),
        "{{BROWSER_LOCK_ENABLED}}": str(getattr(settings, "browser_lock_enabled", True)).lower(),
        "{{CLERK_PUBLISHABLE_KEY}}": settings.clerk_publishable_key,
        "{{CLERK_FRONTEND_API_URL}}": settings.clerk_frontend_api_url,
        "{{APP_URL}}": escape(app_url, quote=True),
        "{{MCP_URL}}": escape(f"{service_url}/mcp", quote=True),
    }
    if filename == "index.html":
        from tin_lite.auth_redirects import auth_return_url

        if len(request.query_params.getlist("redirect_url")) > 1:
            raise HTTPException(status_code=400, detail="Start sign-in again with a fresh link.")
        try:
            return_url, is_mcp = auth_return_url(settings, request.query_params.get("redirect_url"))
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail="Start sign-in again with a fresh link."
            ) from exc
        replacements["{{AUTH_RETURN_URL}}"] = escape(return_url, quote=True)
        replacements["{{AUTH_FLOW}}"] = "mcp" if is_mcp else "product"
    # A return URL can contain marker-like opaque state. Substitute only the
    # original template, never reinterpret text introduced by a replacement.
    html = re.sub(
        r"\{\{[A-Z_]+\}\}|<!--PRIVATE_FONTS_STYLESHEET-->",
        lambda match: replacements.get(match[0], match[0]),
        html,
    )
    headers = {"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"}
    if filename == "index.html":
        headers.update(
            {
                "Referrer-Policy": "strict-origin-when-cross-origin",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": (
                    "frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
                ),
            }
        )
    return HTMLResponse(html, headers=headers)


def _idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 200:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Idempotency-Key must contain between 1 and 200 characters",
        )
    return normalized


_EXPLICIT_MEDIA_TYPES = {
    ".mmd": "text/vnd.mermaid",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".ogv": "video/ogg",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".pdf": "application/pdf",
    ".jsonl": "application/jsonl",
}


def _file_media_type(filename: str) -> str:
    # Explicit entries first: the platform mimetypes table varies by host and returns
    # vendor-prefixed or missing types for several formats the product viewer renders.
    explicit = _EXPLICIT_MEDIA_TYPES.get(PurePosixPath(filename).suffix.casefold())
    if explicit is not None:
        return explicit
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def _project_content_headers(
    filename: str, media_type: str, *, download: bool = False
) -> dict[str, str]:
    # Project bytes are untrusted even at an immutable canonical revision. Only
    # inert formats may open inline; all responses also get a document sandbox.
    inline_types = {
        "text/plain",
        "text/markdown",
        "text/vnd.mermaid",
        "text/csv",
        "text/tab-separated-values",
        "application/json",
        "application/jsonl",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/avif",
        "image/bmp",
        "image/x-icon",
        "image/vnd.microsoft.icon",
    }
    inline = media_type in inline_types or media_type.startswith(("audio/", "video/"))
    disposition = "attachment" if download or not inline else "inline"
    return {
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(filename)}",
        "Content-Security-Policy": (
            "sandbox; default-src 'none'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'"
        ),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
        "Referrer-Policy": "no-referrer",
    }


def _chat_view(
    message: ChatMessage,
    run: WorkflowRun | dict | None,
) -> ChatView:
    return ChatView(
        request_id=message.request_id,
        response_id=message.response_id or "",
        message=message.content,
        routed_workflow_key=message.routed_workflow_key,
        run=RunView.model_validate(run) if run is not None else None,
    )


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    billing_quote_id: UUID | None = None
    inputs: dict = Field(default_factory=dict)
    payment_card: Any = Field(default=None, exclude=True, repr=False)
    instruction: str | None = Field(default=None, min_length=1, max_length=8000)
    title: str | None = Field(default=None, min_length=1, max_length=120)


async def _one_run_card(request: Request):
    if not await request.body():
        return None
    try:
        body = await request.json()
        if not isinstance(body, dict) or set(body) - {"payment_card"}:
            raise ValueError
        return body.get("payment_card")
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid run request.") from None


class RetainedOutputView(BaseModel):
    artifact_path: str
    revision: str = Field(validation_alias="ephemeral_commit_sha")
    media_type: str
    byte_count: int
    reason: Literal[
        "publication_pending", "reconciliation_pending", "output_conflict", "execution_interrupted"
    ]


class RunView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    workflow_id: UUID
    project_workflow_id: UUID | None = None
    workflow_name: str
    definition_commit_sha: str | None
    status: RunStatus
    trigger_source: str = "manual"
    scheduled_for: datetime | None = None
    review_required: bool = False
    review_root_run_id: UUID | None = None
    review_source_run_id: UUID | None = None
    review_version: int = 1
    review_decision: str | None = None
    review_requested_at: datetime | None = None
    reviewed_at: datetime | None = None
    system_wiki_commit_sha: str | None = None
    artifact_ref: str | None
    retained_output: RetainedOutputView | None = None
    output_resolution: dict | None = None
    artifact_path: str | None
    artifact_title: str | None = None
    canonical_commit_sha: str | None
    error_message: str | None
    task_title: str | None = None
    task_phase: str | None = None
    task_summary: str | None = None
    task_question: str | None = None
    task_question_requested_at: datetime | None = None
    task_turn_number: int = 0
    task_result: str | None = None
    task_diff: dict | None = None
    task_has_changes: bool | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    trigger_client: str | None = None
    retry_of_run_id: UUID | None = None
    progress_mode: str = "indeterminate"
    progress_step: str | None = None
    progress_current: int | None = None
    progress_total: int | None = None
    progress_percent: int | None = None
    progress_summary: str | None = None
    progress_updated_at: datetime | None = None
    heartbeat_at: datetime | None = None
    result_summary: str | None = None
    prerequisite_evidence: dict | None = None
    content_delivery: dict | None = None


class OutreachCampaignView(BaseModel):
    run_id: UUID
    project_id: UUID
    channel: str
    status: str
    source_path: str
    source_commit_sha: str
    review_path: str
    review_commit_sha: str
    recipient_count: int
    sent_count: int
    replied_count: int
    failed_count: int
    delivered_count: int
    unknown_delivery_count: int
    failed_delivery_count: int
    daily_send_cap: int
    send_interval_seconds: int
    send_window_start: time
    send_window_end: time
    send_timezone: str
    next_delivery_at: datetime | None
    approved_at: datetime | None
    completed_at: datetime | None
    current_follow_up_body: str | None = None
    pending_revision_id: UUID | None = None
    pending_revision_number: int | None = None
    previous_follow_up_body: str | None = None
    proposed_follow_up_body: str | None = None
    pending_revision_path: str | None = None
    pending_revision_commit_sha: str | None = None
    pending_revision_requested_at: datetime | None = None


class OutreachCampaignDeliveryView(BaseModel):
    recipient_address: str
    recipient_name: str
    recipient_status: str
    step: Literal["initial", "follow_up"]
    status: Literal["pending", "started", "sent", "unknown", "failed", "skipped"]
    scheduled_for: datetime | None = None
    sent_at: datetime | None = None
    replied_at: datetime | None = None


class OutreachCampaignRevisionCreate(BaseModel):
    request_id: UUID
    follow_up_body: str = Field(min_length=1, max_length=20_000)


class OutreachCampaignRevisionView(BaseModel):
    id: UUID
    campaign_run_id: UUID
    project_id: UUID
    request_id: UUID
    revision_number: int
    status: str
    previous_follow_up_body: str
    follow_up_body: str
    review_path: str
    review_commit_sha: str | None
    requested_at: datetime
    reviewed_at: datetime | None


class WorkflowView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID | None
    key: str
    title: str
    description: str
    executor: str
    version_label: str
    current_commit_sha: str | None
    definition: dict
    status: WorkflowStatus
    system_id: str | None
    system_name: str | None
    system_order: int | None
    forked_from_workflow_id: UUID | None
    forked_from_commit_sha: str | None
    saved: bool = False
    project_workflow_count: int = 0
    scope: str = "builtin"
    source: dict = Field(default_factory=dict)
    definition_revision: str | None = None
    input_schema: dict = Field(default_factory=dict)
    allowed_actions: list[str] = Field(default_factory=list)
    runtime_available: bool = True
    prerequisites: list = Field(default_factory=list)
    readiness: dict | None = None


def _workflow_view(workflow, settings):
    return WorkflowView.model_validate(workflow).model_copy(
        update={
            **workflow_source_view(workflow, settings),
            "input_schema": client_input_schema(workflow.definition),
            "prerequisites": workflow.definition.get("prerequisites", []),
        }
    )


class ProjectWorkflowCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: UUID
    name: str = Field(min_length=1, max_length=120)
    inputs: dict = Field(default_factory=dict)
    schedule: WorkflowSchedule | None = None
    request_id: UUID

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return " ".join(value.split())


class ProjectWorkflowUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    inputs: dict = Field(default_factory=dict)
    schedule: WorkflowSchedule | None = None
    expected_settings_revision: int = Field(ge=1)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return " ".join(value.split())


class ProjectWorkflowView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    workflow_id: UUID
    workflow_key: str
    workflow_title: str
    workflow_description: str
    version_label: str
    definition_commit_sha: str
    name: str
    inputs: dict
    input_schema: dict
    schedule: dict | None
    status: str
    next_run_at: datetime | None
    last_run_id: UUID | None
    last_run_status: RunStatus | None
    last_artifact_path: str | None
    last_artifact_title: str | None = None
    last_error: str | None
    settings_revision: int
    created_at: datetime
    updated_at: datetime
    skip_scheduled_for: datetime | None = None
    last_result_summary: str | None = None
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    run_count: int = 0
    done_count: int = 0
    failed_count: int = 0
    typical_duration_seconds: float | None = None
    content_revision: dict | None = None


class ProjectSystemView(BaseModel):
    workflow_count: int
    running_count: int
    runs_this_month: int
    waiting_count: int
    activity_count_30_days: int = 0
    next_run_at: datetime | None = None
    set_up_at: datetime | None = None
    timezone: str
    last_mcp_used_at: datetime | None = None
    last_mcp_tool_name: str | None = None


class DecisionView(BaseModel):
    id: UUID
    run_id: UUID
    project_id: UUID
    workflow_key: str
    workflow_title: str
    kind: Literal["review", "select", "response", "output_conflict"]
    title: str
    explanation: str
    consequence: str
    items: list[dict]
    response_schema: dict
    feedback_supported: bool
    status: str
    deadline_at: datetime | None = None
    created_at: datetime

    output_resolution: dict | None = None


class DecisionApply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["approve"]
    feedback: str | None = Field(default=None, max_length=8000)
    review_token: str | None = Field(default=None, max_length=64)
    # Content drafts only: where this approved document goes, and whether to keep the pick.
    delivery: Literal["github_pr", "github_commit", "none"] | None = None
    remember: bool = False


class WorkflowRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    feedback: str = Field(min_length=1, max_length=8000)
    reference_files: list[str] = Field(default_factory=list, max_length=8)
    request_id: UUID
    review_token: str = Field(min_length=64, max_length=64)
    billing_quote_id: UUID | None = None


class WorkflowReviewApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_token: str | None = Field(default=None, min_length=64, max_length=64)
    # Content drafts only: where this approved document goes, and whether to keep the pick.
    delivery: Literal["github_pr", "github_commit", "none"] | None = None
    remember: bool = False


class ProjectMemoryView(BaseModel):
    project_id: UUID
    scope: str = "project"
    ready: bool
    commit_sha: str | None
    path: str | None
    content: str | None


class ProjectView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    state_repo_id: str
    canonical_branch: str
    timezone: str = "UTC"
    workspace_id: UUID | None = None
    workspace_name: str | None = None
    can_create_project_in_workspace: bool = False
    member_count: int = 1
    can_delete: bool = False


class ProjectDeletionView(BaseModel):
    project_id: UUID
    name: str
    deleted_at: datetime
    stopped_runs: int
    removed_schedules: int
    disconnected: int
    repo_deleted: bool


class WorkspaceView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str


class WorkspaceBootstrapView(BaseModel):
    workspace: WorkspaceView | None
    project: ProjectView


class PersonalProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return normalize_project_name(value)


class WorkspaceProjectCreate(PersonalProjectCreate):
    request_id: UUID


class AuthView(BaseModel):
    clerk_user_id: str
    project_count: int


class ProjectMemberView(BaseModel):
    clerk_user_id: str
    joined_at: datetime


class InvitationCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", normalized):
            raise ValueError("enter a valid email address")
        return normalized


class InvitationView(BaseModel):
    id: UUID
    project_id: UUID
    project_name: str
    email: str
    expires_at: datetime
    invitation_url: str


class ActivityView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    project_id: UUID
    run_id: UUID | None
    event_type: str
    details: dict
    summary: str | None
    audience: str
    created_at: datetime
    workflow_key: str | None
    workflow_title: str | None


class ChatCreate(BaseModel):
    project_id: UUID
    request_id: UUID = Field(default_factory=uuid4)
    message: str = Field(min_length=1, max_length=4000)

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("enter a message")
        return normalized


class ChatView(BaseModel):
    request_id: UUID
    response_id: str
    message: str
    routed_workflow_key: str | None
    run: RunView | None


class ChatMessageView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    request_id: UUID
    role: str
    source: str
    content: str
    author_clerk_user_id: str | None
    response_id: str | None
    routed_workflow_key: str | None
    run_id: UUID | None
    created_at: datetime


class TaskEntryView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    run_id: UUID
    request_id: UUID | None
    kind: str
    source: str
    content: str
    delivered_at: datetime | None
    created_at: datetime


class ProjectTaskView(BaseModel):
    run: RunView
    entries: list[TaskEntryView]


class TaskMessageCreate(BaseModel):
    request_id: UUID = Field(default_factory=uuid4)
    message: str = Field(min_length=1, max_length=8000)

    @field_validator("message")
    @classmethod
    def normalize_task_message(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("enter a message")
        return normalized


class MarkdownHeadingView(BaseModel):
    id: str
    title: str


class MarkdownDocumentView(BaseModel):
    markdown: str
    html: str
    filename: str
    source_url: str
    timestamp: datetime | None
    word_count: int
    reading_minutes: int
    headings: list[MarkdownHeadingView]
    path: str | None = None
    revision: str | None = None
    size_bytes: int | None = None
    related_documents: list[dict[str, str]] = Field(default_factory=list)


class ProjectFileView(BaseModel):
    path: str


class ProjectFilesView(BaseModel):
    project_id: UUID
    revision: str
    files: list[ProjectFileView]


class ProjectFilesCommit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID = Field(default_factory=uuid4)
    expected_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    message: str = Field(min_length=1, max_length=240)
    changes: list[ProjectFileMutationInput] = Field(min_length=1, max_length=50)


class ProjectFilesRevert(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID = Field(default_factory=uuid4)
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    expected_revision: str = Field(pattern=r"^[0-9a-f]{40}$")


class ProjectFileCommitView(BaseModel):
    project_id: UUID
    request_id: UUID
    revision: str
    changed_paths: list[str]
    operation: str
    replayed: bool


class ProjectFileHistoryView(BaseModel):
    revision: str
    message: str
    author_name: str
    date: datetime
    state: str


class ProjectFileSearchLineView(BaseModel):
    line_number: int
    text: str


class ProjectFileSearchMatchView(BaseModel):
    path: str
    lines: list[ProjectFileSearchLineView]


class ProjectFileSearchView(BaseModel):
    revision: str
    matches: list[ProjectFileSearchMatchView]
    has_more: bool


class IntegrationView(BaseModel):
    key: str
    name: str
    badge: str
    description: str
    access_label: str
    capabilities: list[str]
    unlocks: list[str]
    setup_url: str | None = None
    configured: bool
    connection_id: UUID | None = None
    project_id: UUID | None = None
    status: str = "available"
    external_account_label: str | None = None
    configuration: dict = Field(default_factory=dict)
    connected_at: datetime | None = None
    last_checked_at: datetime | None = None
    last_error_code: str | None = None


class IntegrationConnectView(BaseModel):
    authorization_url: str


class IntegrationConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capabilities: list[str] | None = Field(default=None, min_length=1)


class GoogleIntegrationComplete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=1, max_length=4096)


class GitHubIntegrationComplete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=1, max_length=4096)
    installation_id: int | None = Field(default=None, gt=0)
    setup_action: str | None = Field(default=None, max_length=80)


class GitHubIntegrationAuthorize(BaseModel):
    """GitHub came back with an installation but no code: the app was already installed."""

    model_config = ConfigDict(extra="forbid")

    installation_id: int = Field(gt=0)
    setup_action: str | None = Field(default=None, max_length=80)
    state: str | None = Field(default=None, min_length=1, max_length=256)
    project_id: UUID | None = None


class IntegrationOptionView(BaseModel):
    id: str
    label: str
    detail: str | None = None


class IntegrationSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    option_id: str = Field(min_length=1, max_length=500)


class GoogleAdsLinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(min_length=10, max_length=14)


@router.api_route(
    "/", methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False
)
@router.api_route("/system", methods=["GET", "HEAD"], include_in_schema=False)
@router.api_route("/workflows", methods=["GET", "HEAD"], include_in_schema=False)
@router.api_route("/chat", methods=["GET", "HEAD"], include_in_schema=False)
@router.api_route("/activity", methods=["GET", "HEAD"], include_in_schema=False)
@router.api_route("/decisions", methods=["GET", "HEAD"], include_in_schema=False)
@router.api_route("/files", methods=["GET", "HEAD"], include_in_schema=False)
@router.api_route("/integrations", methods=["GET", "HEAD"], include_in_schema=False)
@router.api_route("/billing", methods=["GET", "HEAD"], include_in_schema=False)
@router.api_route("/file", methods=["GET", "HEAD"], include_in_schema=False)
@router.api_route("/document/{run_id:uuid}", methods=["GET", "HEAD"], include_in_schema=False)
@router.api_route("/task/{run_id:uuid}", methods=["GET", "HEAD"], include_in_schema=False)
@router.api_route("/compare/{run_id:uuid}", methods=["GET", "HEAD"], include_in_schema=False)
async def product_ui(request: Request) -> HTMLResponse:
    return _static_page("index.html", request)


@router.api_route(
    "/connect", methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False
)
@router.get("/sign-in", response_class=HTMLResponse, include_in_schema=False)
@router.get("/sign-up", response_class=HTMLResponse, include_in_schema=False)
@router.get("/sign-in/{auth_path:path}", response_class=HTMLResponse, include_in_schema=False)
@router.get("/sign-up/{auth_path:path}", response_class=HTMLResponse, include_in_schema=False)
async def authentication_ui(request: Request) -> HTMLResponse:
    return _static_page("index.html", request)


@router.get("/integrations/callback/google", response_class=HTMLResponse, include_in_schema=False)
@router.get("/integrations/callback/github", response_class=HTMLResponse, include_in_schema=False)
@router.get("/integrations/callback/posthog", response_class=HTMLResponse, include_in_schema=False)
async def integration_callback_ui(request: Request) -> HTMLResponse:
    return _static_page("index.html", request)


@router.get("/integrations/posthog/client.json", include_in_schema=False)
async def posthog_client_metadata(request: Request) -> JSONResponse:
    """Tin's OAuth client identity for PostHog: its URL is the client_id PostHog fetches."""
    from tin_lite.posthog_connection import client_metadata

    runtime = request.app.state.runtime
    if not runtime.integrations.is_configured(POSTHOG_PROVIDER):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    # PostHog caches the document for max-age (clamped to 5 minutes..24 hours).
    return JSONResponse(
        client_metadata(request.app.state.settings),
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.post("/webhooks/github", include_in_schema=False)
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
) -> dict[str, bool]:
    if not x_hub_signature_256 or not x_github_delivery or not x_github_event:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    if len(x_github_delivery) > 200 or len(x_github_event) > 120:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > 1_000_000:
                raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST) from exc
    body = await request.body()
    if len(body) > 1_000_000:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)
    try:
        accepted = await request.app.state.runtime.integrations.handle_github_webhook(
            signature=x_hub_signature_256,
            delivery_id=x_github_delivery,
            event_type=x_github_event,
            body=body,
        )
    except IntegrationNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except IntegrationAuthorizationError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    return {"accepted": accepted}


@router.api_route(
    "/documents/runs/{run_id}",
    methods=["GET", "HEAD"],
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def standalone_markdown_viewer(run_id: UUID, request: Request) -> HTMLResponse:
    del run_id
    return _static_page("viewer.html", request)


@router.get("/healthz")
async def health(request: Request) -> dict[str, str]:
    try:
        healthy = await request.app.state.runtime.database.ping()
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE) from exc
    if not healthy:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return {"status": "ok"}


@router.get("/api/auth/me", response_model=AuthView)
async def auth_me(
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> AuthView:
    projects = await request.app.state.runtime.database.list_projects_for_user(user.clerk_user_id)
    return AuthView(clerk_user_id=user.clerk_user_id, project_count=len(projects))


@router.post("/api/projects/bootstrap", response_model=ProjectView)
async def bootstrap_personal_project(
    payload: PersonalProjectCreate,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> ProjectView:
    try:
        project = await provision_personal_project(
            database=request.app.state.runtime.database,
            storage=request.app.state.runtime.storage,
            clerk_user_id=user.clerk_user_id,
            name=payload.name,
        )
    except ProjectProvisioningError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    return ProjectView.model_validate(project)


@router.get("/api/workspaces", response_model=list[WorkspaceView])
async def list_workspaces(
    request: Request,
    response: Response,
    user: AuthContext = AUTHENTICATED_USER,
) -> list[WorkspaceView]:
    workspaces = await request.app.state.runtime.database.list_workspaces_for_user(
        user.clerk_user_id
    )
    response.headers["X-Tin-Read-Source"] = "postgres"
    return [WorkspaceView.model_validate(item) for item in workspaces]


@router.post("/api/workspaces/bootstrap", response_model=WorkspaceBootstrapView)
async def bootstrap_personal_workspace(
    payload: PersonalProjectCreate,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> WorkspaceBootstrapView:
    try:
        project = await provision_personal_project(
            database=request.app.state.runtime.database,
            storage=request.app.state.runtime.storage,
            clerk_user_id=user.clerk_user_id,
            name=payload.name,
            reuse_existing=False,
        )
    except ProjectProvisioningError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    workspaces: list[Workspace] = await request.app.state.runtime.database.list_workspaces_for_user(
        user.clerk_user_id
    )
    workspace = next((item for item in workspaces if item.id == project.workspace_id), None)
    return WorkspaceBootstrapView(
        workspace=WorkspaceView.model_validate(workspace) if workspace is not None else None,
        project=ProjectView.model_validate(project),
    )


@router.post(
    "/api/workspaces/{workspace_id}/projects",
    response_model=ProjectView,
    status_code=status.HTTP_201_CREATED,
)
async def create_workspace_project(
    workspace_id: UUID,
    payload: WorkspaceProjectCreate,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> ProjectView:
    try:
        project = await provision_workspace_project(
            database=request.app.state.runtime.database,
            storage=request.app.state.runtime.storage,
            workspace_id=workspace_id,
            clerk_user_id=user.clerk_user_id,
            name=payload.name,
            request_id=payload.request_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from exc
    except ProjectCreationConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ProjectProvisioningError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    return ProjectView.model_validate(project)


@router.get(
    "/api/projects/{project_id}/members",
    response_model=list[ProjectMemberView],
)
async def list_project_members(
    project_id: UUID,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> list[ProjectMemberView]:
    await _require_project_access(project_id, request, user)
    members = await request.app.state.runtime.database.list_project_members(project_id)
    return [
        ProjectMemberView(clerk_user_id=member.clerk_user_id, joined_at=member.created_at)
        for member in members
    ]


@router.post(
    "/api/projects/{project_id}/invitations",
    response_model=InvitationView,
    status_code=status.HTTP_201_CREATED,
)
async def create_project_invitation(
    project_id: UUID,
    payload: InvitationCreate,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> InvitationView:
    await _require_project_access(project_id, request, user)
    token = secrets.token_urlsafe(32)
    invitation = await request.app.state.runtime.database.create_project_invitation(
        project_id=project_id,
        email=payload.email,
        token_hash=_token_hash(token),
        created_by_clerk_user_id=user.clerk_user_id,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    public_url = dashboard_url(request.app.state.settings)
    return InvitationView(
        id=invitation.id,
        project_id=invitation.project_id,
        project_name=invitation.project_name,
        email=invitation.email,
        expires_at=invitation.expires_at,
        invitation_url=f"{public_url}/?invite={quote(token)}",
    )


@router.post("/api/invitations/{token}/accept", response_model=ProjectView)
async def accept_project_invitation(
    token: str,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> ProjectView:
    if len(token) > 256:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="invitation not found")
    token_hash = _token_hash(token)
    database = request.app.state.runtime.database
    invitation = await database.get_project_invitation(token_hash)
    if invitation is None or invitation.revoked_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="invitation not found")
    verified_emails = await request.app.state.auth.verified_email_addresses(user.clerk_user_id)
    if invitation.email not in verified_emails:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="sign in with the email address this invitation was sent to",
        )
    try:
        accepted = await database.accept_project_invitation(
            token_hash=token_hash,
            clerk_user_id=user.clerk_user_id,
            expected_email=invitation.email,
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="invitation not found"
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    project = await database.get_project(accepted.project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    return ProjectView.model_validate(project)


@router.get(
    "/api/projects/{project_id}/integrations",
    response_model=list[IntegrationView],
)
async def list_project_integrations(
    project_id: UUID,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> list[IntegrationView]:
    await _require_project_access(project_id, request, user)
    service = request.app.state.runtime.integrations
    connections = {item.provider_key: item for item in await service.list_connections(project_id)}
    return [
        _integration_view(
            definition,
            connections.get(definition.key),
            configured=service.is_configured(definition.key),
        )
        for definition in service.definitions(connections.values())
    ]


@router.post(
    "/api/projects/{project_id}/integrations/{provider_key}/connect",
    response_model=IntegrationConnectView,
)
async def start_integration_connection(
    project_id: UUID,
    provider_key: str,
    request: Request,
    payload: IntegrationConnectRequest | None = None,
    user: AuthContext = AUTHENTICATED_USER,
) -> IntegrationConnectView:
    await _require_project_access(project_id, request, user)
    try:
        started = await request.app.state.runtime.integrations.start_connect(
            project_id=project_id,
            provider_key=provider_key,
            clerk_user_id=user.clerk_user_id,
            capabilities=payload.capabilities if payload is not None else None,
        )
    except IntegrationError as exc:
        raise _integration_http_error(exc) from exc
    return IntegrationConnectView(authorization_url=started.authorization_url)


@router.post(
    "/api/integrations/google/complete",
    response_model=IntegrationView,
)
async def complete_google_integration(
    payload: GoogleIntegrationComplete,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> IntegrationView:
    service = request.app.state.runtime.integrations
    try:
        attempt = await service.pending_google(
            state=payload.state, clerk_user_id=user.clerk_user_id
        )
        await _require_project_access(attempt.project_id, request, user)
        connection = await service.complete_google(
            state=payload.state,
            code=payload.code,
            clerk_user_id=user.clerk_user_id,
        )
    except IntegrationError as exc:
        raise _integration_http_error(exc) from exc
    definition = next(
        item for item in registered_integrations() if item.key == connection.provider_key
    )
    return _integration_view(definition, connection, configured=True)


@router.post(
    "/api/integrations/posthog/complete",
    response_model=IntegrationView,
)
async def complete_posthog_integration(
    payload: GoogleIntegrationComplete,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> IntegrationView:
    """Finish PostHog OAuth; a repeated callback returns the connection it already made."""
    service = request.app.state.runtime.integrations
    try:
        project_id = await service.posthog.pending_project(
            state=payload.state, clerk_user_id=user.clerk_user_id
        )
        await _require_project_access(project_id, request, user)
        connection = await service.posthog.complete(
            state=payload.state, code=payload.code, clerk_user_id=user.clerk_user_id
        )
    except IntegrationError as exc:
        raise _integration_http_error(exc) from None
    return _integration_view(service._definition(POSTHOG_PROVIDER), connection, configured=True)


@router.post(
    "/api/integrations/github/authorize",
    response_model=IntegrationConnectView,
)
async def authorize_github_installation(
    payload: GitHubIntegrationAuthorize,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> IntegrationConnectView:
    service = request.app.state.runtime.integrations
    project_id = payload.project_id
    if payload.state is not None:
        try:
            project_id = await service.pending_project(
                state=payload.state,
                provider_key=GITHUB_PROVIDER,
                clerk_user_id=user.clerk_user_id,
            )
        except IntegrationAuthorizationError:
            if project_id is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Start the GitHub connection from your project again",
                ) from None
    if project_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Start the GitHub connection from your project again",
        )
    await _require_project_access(project_id, request, user)
    try:
        started = await service.start_github_authorization(
            project_id=project_id,
            installation_id=payload.installation_id,
            setup_action=payload.setup_action,
            clerk_user_id=user.clerk_user_id,
            state=payload.state,
        )
    except IntegrationError as exc:
        raise _integration_http_error(exc) from exc
    return IntegrationConnectView(authorization_url=started.authorization_url)


@router.post(
    "/api/integrations/github/complete",
    response_model=IntegrationView,
)
async def complete_github_integration(
    payload: GitHubIntegrationComplete,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> IntegrationView:
    service = request.app.state.runtime.integrations
    try:
        project_id = await service.pending_project(
            state=payload.state,
            provider_key=GITHUB_PROVIDER,
            clerk_user_id=user.clerk_user_id,
        )
        await _require_project_access(project_id, request, user)
        connection = await service.complete_github(
            state=payload.state,
            code=payload.code,
            installation_id=payload.installation_id,
            setup_action=payload.setup_action,
            clerk_user_id=user.clerk_user_id,
        )
    except GitHubInstallationRequiredError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "github_install_required",
                "message": str(exc),
                "install_url": exc.install_url,
                "project_id": str(exc.project_id),
            },
        ) from exc
    except GitHubInstallationChoiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "github_installation_choice",
                "message": str(exc),
                "choices": exc.choices,
                "project_id": str(exc.project_id),
            },
        ) from exc
    except IntegrationError as exc:
        raise _integration_http_error(exc) from exc
    definition = next(item for item in registered_integrations() if item.key == GITHUB_PROVIDER)
    return _integration_view(definition, connection, configured=True)


@router.get(
    "/api/projects/{project_id}/integrations/{provider_key}/options",
    response_model=list[IntegrationOptionView],
)
async def list_integration_options(
    project_id: UUID,
    provider_key: str,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> list[IntegrationOptionView]:
    await _require_project_access(project_id, request, user)
    service = request.app.state.runtime.integrations
    try:
        if provider_key == GSC_PROVIDER:
            options = await service.google_sites(project_id=project_id)
        elif provider_key == GITHUB_PROVIDER:
            options = await service.github_repositories(project_id=project_id)
        elif provider_key == POSTHOG_PROVIDER:
            options = await service.posthog.projects(project_id=project_id)
        elif provider_key == GOOGLE_WORKSPACE_PROVIDER:
            raise IntegrationAuthorizationError(
                "Google Workspace connects an account and has no selectable property"
            )
        elif provider_key == ADS_PROVIDER:
            raise IntegrationAuthorizationError(
                "Google Ads links one account by customer id and has no selectable property"
            )
        elif provider_key == STRIPE_PROVIDER:
            raise IntegrationAuthorizationError(
                "Stripe connects one account by restricted key and has no selectable property"
            )
        else:
            raise IntegrationAuthorizationError("unknown integration provider")
    except IntegrationError as exc:
        raise _integration_http_error(exc) from exc
    return [IntegrationOptionView.model_validate(item, from_attributes=True) for item in options]


@router.post(
    "/api/projects/{project_id}/integrations/ads.google/link",
    response_model=IntegrationView,
)
async def link_google_ads_account(
    project_id: UUID,
    payload: GoogleAdsLinkRequest,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> IntegrationView:
    """Record the founder's Google Ads customer id and send Tin's manager invitation."""
    await _require_project_access(project_id, request, user)
    service = request.app.state.runtime.integrations
    try:
        connection = await service.connect_google_ads(
            project_id=project_id,
            customer_id=payload.customer_id,
            clerk_user_id=user.clerk_user_id,
        )
    except IntegrationError as exc:
        raise _integration_http_error(exc) from exc
    definition = next(item for item in registered_integrations() if item.key == ADS_PROVIDER)
    return _integration_view(definition, connection, configured=True)


@router.post(
    "/api/projects/{project_id}/integrations/ads.google/refresh",
    response_model=IntegrationView,
)
async def refresh_google_ads_account(
    project_id: UUID,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> IntegrationView:
    """Re-read the manager link, then billing and conversion health once it is active."""
    await _require_project_access(project_id, request, user)
    service = request.app.state.runtime.integrations
    try:
        connection = await service.refresh_google_ads(project_id=project_id)
    except IntegrationError as exc:
        raise _integration_http_error(exc) from exc
    definition = next(item for item in registered_integrations() if item.key == ADS_PROVIDER)
    return _integration_view(definition, connection, configured=True)


@router.post(
    "/api/projects/{project_id}/integrations/payments.stripe/key",
    response_model=IntegrationView,
)
async def save_stripe_key(
    project_id: UUID,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> IntegrationView:
    """Validate and store a pasted Stripe restricted key; replacing one names its revision."""
    await _require_project_access(project_id, request, user)
    # Parsed by hand: FastAPI's validation details would echo a malformed key back.
    try:
        payload = await request.json()
        if not isinstance(payload, dict) or set(payload) - {"restricted_key", "expected_revision"}:
            raise ValueError
        key, revision = payload.get("restricted_key"), payload.get("expected_revision")
        if not isinstance(key, str) or not 1 <= len(key) <= 300:
            raise ValueError
        if revision is not None and (not isinstance(revision, str) or len(revision) > 64):
            raise ValueError
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(
            status_code=422,
            detail="Send restricted_key and expected_revision; nothing was saved.",
        ) from None
    service = request.app.state.runtime.integrations
    try:
        connection = await service.stripe.connect(
            project_id=project_id,
            clerk_user_id=user.clerk_user_id,
            restricted_key=key,
            expected_revision=revision,
        )
    except IntegrationError as exc:
        raise _integration_http_error(exc) from None
    return _integration_view(service._definition(STRIPE_PROVIDER), connection, configured=True)


@router.post(
    "/api/projects/{project_id}/integrations/payments.stripe/refresh",
    response_model=IntegrationView,
)
async def refresh_stripe_key(
    project_id: UUID,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> IntegrationView:
    """Re-check which reads the stored key allows, after it was edited in Stripe."""
    await _require_project_access(project_id, request, user)
    service = request.app.state.runtime.integrations
    try:
        connection = await service.stripe.refresh(project_id=project_id)
    except IntegrationError as exc:
        raise _integration_http_error(exc) from exc
    return _integration_view(service._definition(STRIPE_PROVIDER), connection, configured=True)


@router.put(
    "/api/projects/{project_id}/integrations/{provider_key}",
    response_model=IntegrationView,
)
async def configure_project_integration(
    project_id: UUID,
    provider_key: str,
    payload: IntegrationSelection,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> IntegrationView:
    await _require_project_access(project_id, request, user)
    service = request.app.state.runtime.integrations
    try:
        connection = await service.select_option(
            project_id=project_id,
            provider_key=provider_key,
            option_id=payload.option_id,
        )
    except IntegrationError as exc:
        raise _integration_http_error(exc) from exc
    definition = next(
        (item for item in registered_integrations() if item.key == provider_key), None
    )
    if definition is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="integration not found")
    return _integration_view(definition, connection, configured=True)


@router.delete(
    "/api/projects/{project_id}/integrations/{provider_key}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def disconnect_project_integration(
    project_id: UUID,
    provider_key: str,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> Response:
    await _require_project_access(project_id, request, user)
    try:
        deleted = await request.app.state.runtime.integrations.disconnect(
            project_id=project_id, provider_key=provider_key
        )
    except IntegrationError as exc:
        raise _integration_http_error(exc) from exc
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="integration not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/api/chat", response_model=ChatView)
async def chat(
    payload: ChatCreate,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> ChatView:
    await _require_project_access(payload.project_id, request, user)
    runtime = request.app.state.runtime
    database = runtime.database
    luna = runtime.luna
    if luna is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Luna is not configured",
        )
    if user.session_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="an active Clerk session is required",
        )
    async with database.chat_request_lock(
        project_id=payload.project_id,
        request_id=payload.request_id,
    ):
        try:
            await database.create_chat_user_message(
                project_id=payload.project_id,
                request_id=payload.request_id,
                content=payload.message,
                author_clerk_user_id=user.clerk_user_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

        existing = await database.get_chat_assistant_message(
            project_id=payload.project_id,
            request_id=payload.request_id,
        )
        if existing is not None:
            existing_run = await database.get_run(existing.run_id) if existing.run_id else None
            return _chat_view(existing, existing_run)

        context = await database.list_chat_context_messages(
            project_id=payload.project_id,
            exclude_request_id=payload.request_id,
            turn_limit=6,
        )
        continuation_token = await request.app.state.auth.fresh_session_token(user.session_id)
        try:
            result = await luna.respond(
                project_id=payload.project_id,
                message=payload.message,
                conversation=[{"role": item.role, "content": item.content} for item in context],
                authorization=f"Bearer {continuation_token}",
                idempotency_key=f"chat:{payload.request_id}",
            )
        except LunaSafetyError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Luna proposed an invalid workflow action",
            ) from exc
        except (LunaProtocolError, LunaUpstreamError) as exc:
            logger.exception(
                "Luna request failed for project %s and chat request %s",
                payload.project_id,
                payload.request_id,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Luna could not complete the request",
            ) from exc
        run_id = UUID(str(result.run["id"])) if result.run is not None else None
        assistant = await database.create_chat_assistant_message(
            project_id=payload.project_id,
            request_id=payload.request_id,
            content=result.message,
            response_id=result.response_id,
            routed_workflow_key=result.routed_workflow_key,
            run_id=run_id,
        )
        return _chat_view(
            assistant,
            result.run,
        )


@router.get(
    "/api/projects/{project_id}/chat/messages",
    response_model=list[ChatMessageView],
)
async def list_project_chat_messages(
    project_id: UUID,
    request: Request,
    response: Response,
    limit: int = Query(default=100, ge=1, le=200),
    user: AuthContext = AUTHENTICATED_USER,
) -> list[ChatMessageView]:
    await _require_project_access(project_id, request, user)
    messages = await request.app.state.runtime.database.list_chat_messages(
        project_id=project_id,
        limit=limit,
    )
    response.headers["X-Tin-Read-Source"] = "postgres"
    return [ChatMessageView.model_validate(item) for item in messages]


@router.get("/api/workflows", response_model=list[WorkflowView])
async def list_workflows(
    request: Request,
    response: Response,
    project_id: UUID | None = None,
    user: AuthContext = AUTHENTICATED_USER,
) -> list[WorkflowView]:
    if project_id is not None:
        await _require_project_access(project_id, request, user)
    workflows = await request.app.state.runtime.database.list_workflows(project_id=project_id)
    template_state = (
        await request.app.state.runtime.database.get_workflow_template_state(
            project_id=project_id,
            clerk_user_id=user.clerk_user_id,
        )
        if project_id is not None
        else {}
    )
    visible = [
        item
        for item in workflows
        # Agent-only workflows (start here) run through the MCP; the catalog does not list them.
        if not (item.definition or {}).get("agent_only")
        and (item.definition or {}).get("public_discovery", True)
        and (
            item.project_id is None
            or private_execution_ready(request.app.state.settings, item.project_id)
        )
    ]
    readiness = (
        await project_readiness(
            database=request.app.state.runtime.database,
            storage=getattr(request.app.state.runtime, "storage", None),
            project_id=project_id,
            workflows=visible,
        )
        if project_id is not None
        else {}
    )
    response.headers["X-Tin-Read-Source"] = "postgres"
    return [
        _workflow_view(item, request.app.state.settings).model_copy(
            update={
                **template_state.get(item.id, {"saved": False, "project_workflow_count": 0}),
                "readiness": readiness.get(item.id),
            }
        )
        for item in visible
    ]


@router.put(
    "/api/projects/{project_id}/workflow-templates/{workflow_id}/saved",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def save_workflow_template(
    project_id: UUID,
    workflow_id: UUID,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> Response:
    await _require_project_access(project_id, request, user)
    workflow = await request.app.state.runtime.database.get_workflow(workflow_id)
    if workflow is None or (workflow.project_id is not None and workflow.project_id != project_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workflow not found")
    await request.app.state.runtime.database.save_workflow_template(
        workflow_id=workflow_id,
        clerk_user_id=user.clerk_user_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/api/projects/{project_id}/workflow-templates/{workflow_id}/saved",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_saved_workflow_template(
    project_id: UUID,
    workflow_id: UUID,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> Response:
    await _require_project_access(project_id, request, user)
    await request.app.state.runtime.database.remove_saved_workflow_template(
        workflow_id=workflow_id,
        clerk_user_id=user.clerk_user_id,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/api/projects", response_model=list[ProjectView])
async def list_projects(
    request: Request,
    response: Response,
    user: AuthContext = AUTHENTICATED_USER,
) -> list[ProjectView]:
    projects = await request.app.state.runtime.database.list_projects_for_user(user.clerk_user_id)
    response.headers["X-Tin-Read-Source"] = "postgres"
    return [
        ProjectView.model_validate(item).model_copy(
            update={"can_delete": can_delete_project(item, user.clerk_user_id)}
        )
        for item in projects
    ]


@router.delete("/api/projects/{project_id}", response_model=ProjectDeletionView)
async def delete_project(
    project_id: UUID,
    request: Request,
    request_id: UUID,
    confirm_name: str,
    user: AuthContext = AUTHENTICATED_USER,
) -> ProjectDeletionView:
    """Delete a project the caller created: stop its work, drop its schedules, connections
    and files, and hide it from every member. The row and billing history stay.
    """
    runtime = request.app.state.runtime
    # Access first, so a wrong name never confirms that a project exists.
    try:
        project = await deletable_project(runtime, project_id=project_id, actor=user.clerk_user_id)
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    try:
        confirmed = normalize_project_name(confirm_name)
    except ValueError:
        confirmed = ""
    if confirmed != project.name:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="confirm_name does not match the project name",
        )
    try:
        result = await delete_project_service(
            runtime, project_id=project_id, actor=user.clerk_user_id, request_id=request_id
        )
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project not found"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except SideEffectConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ProjectDeletionPending as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    return ProjectDeletionView(**result)


@router.get(
    "/api/projects/{project_id}/files",
    response_model=ProjectFilesView,
)
async def list_project_files(
    project_id: UUID,
    request: Request,
    revision: str | None = Query(default=None, pattern=r"^[0-9a-f]{40}$"),
    user: AuthContext = AUTHENTICATED_USER,
) -> ProjectFilesView:
    project = await _require_project_access(project_id, request, user)
    try:
        if revision is None:
            paths, resolved_revision = await request.app.state.runtime.storage.list_canonical_files(
                repo_id=project.state_repo_id,
                branch=project.canonical_branch,
            )
        else:
            paths = await request.app.state.runtime.storage.list_canonical_files_at(
                repo_id=project.state_repo_id,
                revision=revision,
            )
            resolved_revision = revision
    except LookupError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="project files are not available",
        ) from exc
    return ProjectFilesView(
        project_id=project.id,
        revision=resolved_revision,
        files=[ProjectFileView(path=path) for path in paths],
    )


@router.get(
    "/api/projects/{project_id}/files/search",
    response_model=ProjectFileSearchView,
)
async def search_project_files(
    project_id: UUID,
    request: Request,
    query: str = Query(min_length=1, max_length=500),
    revision: str | None = Query(default=None, pattern=r"^[0-9a-f]{40}$"),
    path: list[str] | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    user: AuthContext = AUTHENTICATED_USER,
) -> ProjectFileSearchView:
    project = await _require_project_access(project_id, request, user)
    if path and any(not safe_project_file_path(item) for item in path):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="unsafe path")
    if revision is None:
        _, revision = await request.app.state.runtime.storage.list_canonical_files(
            repo_id=project.state_repo_id,
            branch=project.canonical_branch,
        )
    try:
        matches, has_more = await request.app.state.runtime.storage.search_canonical_files(
            repo_id=project.state_repo_id,
            revision=revision,
            query=query,
            paths=path,
            limit=limit,
        )
    except Exception as exc:
        storage_status = getattr(exc, "status_code", None)
        response_status = getattr(getattr(exc, "response", None), "status_code", None)
        if storage_status == 404 or response_status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="revision not found"
            ) from exc
        raise
    return ProjectFileSearchView(revision=revision, matches=matches, has_more=has_more)


@router.get(
    "/api/projects/{project_id}/files/history",
    response_model=list[ProjectFileHistoryView],
)
async def get_project_file_history(
    project_id: UUID,
    request: Request,
    path: str = Query(min_length=1, max_length=512),
    limit: int = Query(default=25, ge=1, le=50),
    user: AuthContext = AUTHENTICATED_USER,
) -> list[ProjectFileHistoryView]:
    project = await _require_project_access(project_id, request, user)
    if not safe_project_file_path(path):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="unsafe path")
    values = await request.app.state.runtime.storage.canonical_file_history(
        repo_id=project.state_repo_id,
        branch=project.canonical_branch,
        path=path,
        limit=limit,
    )
    return [ProjectFileHistoryView.model_validate(item) for item in values]


@router.post(
    "/api/projects/{project_id}/files/commit",
    response_model=ProjectFileCommitView,
)
async def commit_project_files(
    project_id: UUID,
    payload: ProjectFilesCommit,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> ProjectFileCommitView:
    project = await _require_project_access(project_id, request, user)
    try:
        result = await request.app.state.runtime.project_files.commit(
            project=project,
            actor_clerk_user_id=user.clerk_user_id,
            client_id="browser",
            request_id=payload.request_id,
            expected_revision=payload.expected_revision,
            message=payload.message,
            changes=[item.model_dump() for item in payload.changes],
        )
    except (StaleProjectRevisionError, SideEffectConflictError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return ProjectFileCommitView(
        project_id=result.project_id,
        request_id=result.request_id,
        revision=result.revision,
        changed_paths=list(result.changed_paths),
        operation=result.operation,
        replayed=result.replayed,
    )


@router.post(
    "/api/projects/{project_id}/files/revert",
    response_model=ProjectFileCommitView,
)
async def revert_project_files(
    project_id: UUID,
    payload: ProjectFilesRevert,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> ProjectFileCommitView:
    project = await _require_project_access(project_id, request, user)
    try:
        result = await request.app.state.runtime.project_files.revert_latest(
            project=project,
            actor_clerk_user_id=user.clerk_user_id,
            client_id="browser",
            request_id=payload.request_id,
            commit_sha=payload.commit_sha,
            expected_revision=payload.expected_revision,
        )
    except (StaleProjectRevisionError, SideEffectConflictError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return ProjectFileCommitView(
        project_id=result.project_id,
        request_id=result.request_id,
        revision=result.revision,
        changed_paths=list(result.changed_paths),
        operation=result.operation,
        replayed=result.replayed,
    )


@router.get("/api/projects/{project_id}/files/raw")
async def get_project_file(
    project_id: UUID,
    request: Request,
    path: str = Query(min_length=1, max_length=1024),
    revision: str = Query(pattern=r"^[0-9a-f]{40}$"),
    download: bool = False,
    user: AuthContext = AUTHENTICATED_USER,
) -> Response:
    _, content = await _read_project_file(
        project_id=project_id,
        path=path,
        revision=revision,
        request=request,
        user=user,
    )
    filename = PurePosixPath(path).name
    media_type = _file_media_type(filename)
    return Response(
        content=content,
        media_type=media_type,
        headers={
            **_project_content_headers(filename, media_type, download=download),
            "X-Tin-File-Source": "code.storage",
            "X-Tin-File-Revision": revision,
        },
    )


@router.get(
    "/api/projects/{project_id}/files/document",
    response_model=MarkdownDocumentView,
)
async def get_project_file_document(
    project_id: UUID,
    request: Request,
    response: Response,
    path: str = Query(min_length=1, max_length=1024),
    revision: str = Query(pattern=r"^[0-9a-f]{40}$"),
    user: AuthContext = AUTHENTICATED_USER,
) -> MarkdownDocumentView:
    if PurePosixPath(path).suffix.casefold() not in {".md", ".markdown"}:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="project file is not Markdown",
        )
    _, content = await _read_project_file(
        project_id=project_id,
        path=path,
        revision=revision,
        request=request,
        user=user,
    )
    try:
        markdown = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Markdown project file is not UTF-8",
        ) from exc
    document = render_markdown(markdown)
    source_query = f"path={quote(path, safe='')}&revision={revision}"
    response.headers["X-Tin-File-Source"] = "code.storage"
    response.headers["X-Tin-File-Revision"] = revision
    return MarkdownDocumentView(
        markdown=document.markdown,
        html=document.html,
        filename=PurePosixPath(path).name,
        source_url=f"/api/projects/{project_id}/files/raw?{source_query}",
        timestamp=None,
        word_count=document.word_count,
        reading_minutes=document.reading_minutes,
        headings=[
            MarkdownHeadingView(id=heading.id, title=heading.title) for heading in document.headings
        ],
        path=path,
        revision=revision,
        size_bytes=len(content),
    )


@router.get(
    "/api/projects/{project_id}/workflows",
    response_model=list[ProjectWorkflowView],
)
async def list_project_workflows(
    project_id: UUID,
    request: Request,
    response: Response,
    user: AuthContext = AUTHENTICATED_USER,
) -> list[ProjectWorkflowView]:
    await _require_project_access(project_id, request, user)
    configured = await request.app.state.runtime.database.list_project_workflows(
        project_id=project_id
    )
    response.headers["X-Tin-Read-Source"] = "postgres"
    return [ProjectWorkflowView.model_validate(item) for item in configured]


class WorkflowSetupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_id: UUID | None = None
    project_workflow_id: UUID | None = None
    inputs: dict[str, Any] | None = None


@router.post("/api/projects/{project_id}/workflow-setup")
async def workflow_setup(
    project_id: UUID,
    payload: WorkflowSetupRequest,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
):
    from tin_lite.workflow_setup import prepare_workflow

    await _require_project_access(project_id, request, user)
    try:
        return await prepare_workflow(
            runtime=request.app.state.runtime,
            settings=request.app.state.settings,
            project_id=project_id,
            actor=user.clerk_user_id,
            **payload.model_dump(),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/projects/{project_id}/system", response_model=ProjectSystemView)
async def get_project_system(
    project_id: UUID,
    request: Request,
    response: Response,
    user: AuthContext = AUTHENTICATED_USER,
) -> ProjectSystemView:
    await _require_project_access(project_id, request, user)
    summary = await request.app.state.runtime.database.get_project_system_summary(
        project_id=project_id
    )
    response.headers["X-Tin-Read-Source"] = "postgres"
    return ProjectSystemView.model_validate(summary)


@router.get(
    "/api/projects/{project_id}/decisions",
    response_model=list[DecisionView],
)
async def list_project_decisions(
    project_id: UUID,
    request: Request,
    response: Response,
    user: AuthContext = AUTHENTICATED_USER,
) -> list[DecisionView]:
    await _require_project_access(project_id, request, user)
    decisions = await request.app.state.runtime.database.list_pending_decisions(
        project_id=project_id
    )
    response.headers["X-Tin-Read-Source"] = "postgres"
    return [DecisionView.model_validate(item) for item in decisions]


@router.post(
    "/api/decisions/{decision_id}/apply",
    response_model=RunView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def apply_decision(
    decision_id: UUID,
    payload: DecisionApply,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> RunView:
    database = request.app.state.runtime.database
    decision = await database.get_pending_decision(decision_id=decision_id)
    if decision is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="decision not found")
    await _require_project_access(decision["project_id"], request, user)
    if payload.feedback:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Use Request changes to revise the draft. Approval does not apply feedback.",
        )
    if decision["workflow_name"] == PROJECT_TASK_WORKFLOW_NAME:
        result = await approve_project_task(decision["run_id"], request, user)
    else:
        reviewed_run = await database.get_run(decision["run_id"])
        if reviewed_run.review_version > 1 and payload.review_token is None:
            raise HTTPException(status_code=409, detail="Read the revised draft before approving.")
        result = await approve_run(
            decision["run_id"],
            request,
            user,
            WorkflowReviewApproval(
                review_token=payload.review_token,
                delivery=payload.delivery,
                remember=payload.remember,
            ),
        )
    await database.apply_run_decision(
        decision_id=decision_id,
        clerk_user_id=user.clerk_user_id,
        response={"action": payload.action, "feedback": payload.feedback},
    )
    return result


@router.post(
    "/api/projects/{project_id}/workflows",
    response_model=ProjectWorkflowView,
    status_code=status.HTTP_201_CREATED,
)
async def create_project_workflow(
    project_id: UUID,
    payload: ProjectWorkflowCreate,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> ProjectWorkflowView:
    await _require_project_access(project_id, request, user)
    database = request.app.state.runtime.database
    workflow = await database.get_workflow(payload.workflow_id)
    if workflow is None or (workflow.project_id is not None and workflow.project_id != project_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workflow not found")
    from tin_lite.workflow_definitions import resolve_execution_contract

    try:
        workflow = await resolve_execution_contract(
            storage=getattr(request.app.state.runtime, "storage", None),
            workflow=workflow,
            project_id=project_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if workflow.definition.get("kind", "workflow") != "workflow":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="one-off tasks cannot be saved or scheduled as project workflows",
        )
    schema = workflow.definition.get("input_schema")
    if not isinstance(schema, dict) or workflow.current_commit_sha is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="workflow definition is not configurable",
        )
    try:
        _ensure_workflow_schedule_allowed(workflow.definition, payload.schedule)
        inputs = normalize_workflow_inputs(
            schema=schema,
            project_id=project_id,
            inputs=payload.inputs,
        )
        configured = await database.create_project_workflow(
            project_id=project_id,
            workflow_id=workflow.id,
            definition_commit_sha=workflow.current_commit_sha,
            name=payload.name,
            inputs=inputs,
            input_schema=schema,
            schedule=payload.schedule.model_dump(mode="json") if payload.schedule else None,
            request_id=payload.request_id,
            created_by_clerk_user_id=user.clerk_user_id,
            pinned_definition=workflow.definition,
        )
        configured = await _sync_project_workflow_schedule(configured, request)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return ProjectWorkflowView.model_validate(configured)


@router.put(
    "/api/projects/{project_id}/workflows/{project_workflow_id}",
    response_model=ProjectWorkflowView,
)
async def update_project_workflow(
    project_id: UUID,
    project_workflow_id: UUID,
    payload: ProjectWorkflowUpdate,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> ProjectWorkflowView:
    await _require_project_access(project_id, request, user)
    database = request.app.state.runtime.database
    existing = await database.get_project_workflow(project_workflow_id)
    if existing is None or existing.project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project workflow not found"
        )
    workflow = await database.get_workflow(existing.workflow_id)
    if workflow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workflow not found")
    try:
        from tin_lite.workflow_definitions import resolve_execution_contract

        workflow = await resolve_execution_contract(
            storage=getattr(request.app.state.runtime, "storage", None),
            workflow=workflow,
            project_id=project_id,
            revision=existing.definition_commit_sha,
            input_schema=existing.input_schema,
        )
        _ensure_workflow_schedule_allowed(workflow.definition, payload.schedule)
        inputs = normalize_workflow_inputs(
            schema=existing.input_schema,
            project_id=project_id,
            inputs=payload.inputs,
        )
        schedule = payload.schedule.model_dump(mode="json") if payload.schedule else None
        changed_fields = _changed_project_workflow_fields(
            existing=existing,
            name=payload.name,
            inputs=inputs,
            schedule=schedule,
        )
        if not changed_fields:
            return ProjectWorkflowView.model_validate(existing)
        configured = await database.update_project_workflow(
            project_workflow_id=project_workflow_id,
            project_id=project_id,
            name=payload.name,
            inputs=inputs,
            schedule=schedule,
            expected_settings_revision=payload.expected_settings_revision,
            clerk_user_id=user.clerk_user_id,
            changed_fields=changed_fields,
            workflow_key=existing.workflow_key,
            workflow_title=existing.workflow_title,
        )
        configured = await _sync_project_workflow_schedule(
            configured,
            request,
            previous_schedule=existing.schedule,
            paused=existing.status == "paused",
        )
    except StaleSettingsRevisionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return ProjectWorkflowView.model_validate(configured)


def _changed_project_workflow_fields(
    *,
    existing: ProjectWorkflow,
    name: str,
    inputs: dict,
    schedule: dict | None,
) -> list[str]:
    changed: list[str] = []
    if existing.name != name:
        changed.append("name")
    input_names = sorted(set(existing.inputs) | set(inputs))
    changed.extend(
        input_name.replace("_", " ")
        for input_name in input_names
        if existing.inputs.get(input_name) != inputs.get(input_name)
    )
    if existing.schedule != schedule:
        changed.append("schedule")
    return changed


def _ensure_workflow_schedule_allowed(definition: dict, schedule: WorkflowSchedule | None) -> None:
    from tin_lite.workflow_definitions import ensure_schedule_allowed

    ensure_schedule_allowed(definition, schedule)


@router.post(
    "/api/projects/{project_id}/workflows/{project_workflow_id}/runs",
    response_model=RunView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_project_workflow_run(
    project_id: UUID,
    project_workflow_id: UUID,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    billing_quote_id: UUID | None = BILLING_QUOTE_HEADER,
    user: AuthContext = AUTHENTICATED_USER,
) -> RunView:
    await _require_project_access(project_id, request, user)
    database = request.app.state.runtime.database
    configured = await database.get_project_workflow(project_workflow_id)
    if configured is None or configured.project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project workflow not found"
        )
    workflow = await database.get_workflow(configured.workflow_id)
    if workflow is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="workflow is unavailable")
    try:
        run = await dispatch_workflow_run(
            runtime=request.app.state.runtime,
            settings=request.app.state.settings,
            workflow=workflow,
            project_id=project_id,
            started_by_clerk_user_id=user.clerk_user_id,
            start_idempotency_key=_idempotency_key(idempotency_key),
            input_payload=configured.inputs,
            payment_card=await _one_run_card(request),
            project_workflow_id=configured.id,
            definition_commit_sha=configured.definition_commit_sha,
            input_schema=configured.input_schema,
            billing_quote_id=billing_quote_id,
            **_run_start_provenance(user),
        )
    except BillingError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.diagnostic()) from exc
    except PrerequisiteError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.diagnostic()) from exc
    except TemporalStartError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "run_id": str(exc.run_id),
                "message": str(exc),
            },
        ) from exc
    except IntegrationError as exc:
        raise _integration_http_error(exc) from exc
    except (LookupError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return RunView.model_validate(run)


@router.post(
    "/api/projects/{project_id}/workflows/{project_workflow_id}/retry",
    response_model=RunView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_project_workflow_run(
    project_id: UUID,
    project_workflow_id: UUID,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    billing_quote_id: UUID | None = BILLING_QUOTE_HEADER,
    user: AuthContext = AUTHENTICATED_USER,
) -> RunView:
    await _require_project_access(project_id, request, user)
    database = request.app.state.runtime.database
    configured = await database.get_project_workflow(project_workflow_id)
    if configured is None or configured.project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project workflow not found"
        )
    # A lost reply can follow creation of the retry run, which is no longer failed.
    # Recover that exact request before applying the "failed last run" precondition.
    start_key = _idempotency_key(idempotency_key)
    existing = (
        await database.get_run_by_start_key(project_id=project_id, start_idempotency_key=start_key)
        if start_key
        else None
    )
    if existing is not None:
        provenance = {
            "trigger_source": "manual",
            "trigger_client": None,
            "started_by_oauth_client_id": None,
            **_run_start_provenance(user),
        }
        if (
            existing.project_workflow_id != project_workflow_id
            or existing.started_by_clerk_user_id != user.clerk_user_id
            or existing.retry_of_run_id is None
            or any(getattr(existing, key) != value for key, value in provenance.items())
        ):
            raise HTTPException(status_code=409, detail="Retry request belongs to another start.")
        return RunView.model_validate(existing)
    if configured.last_run_id is None or configured.last_run_status != RunStatus.FAILED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="this workflow has no failed run to retry",
        )
    workflow = await database.get_workflow(configured.workflow_id)
    if workflow is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="workflow is unavailable")
    try:
        run = await dispatch_workflow_run(
            runtime=request.app.state.runtime,
            settings=request.app.state.settings,
            workflow=workflow,
            project_id=project_id,
            started_by_clerk_user_id=user.clerk_user_id,
            start_idempotency_key=_idempotency_key(idempotency_key),
            input_payload=configured.inputs,
            project_workflow_id=configured.id,
            definition_commit_sha=configured.definition_commit_sha,
            input_schema=configured.input_schema,
            retry_of_run_id=configured.last_run_id,
            billing_quote_id=billing_quote_id,
            **_run_start_provenance(user),
        )
    except BillingError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.diagnostic()) from exc
    except PrerequisiteError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.diagnostic()) from exc
    except TemporalStartError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "run_id": str(exc.run_id),
                "message": str(exc),
            },
        ) from exc
    except IntegrationError as exc:
        raise _integration_http_error(exc) from exc
    except (LookupError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return RunView.model_validate(run)


@router.post(
    "/api/projects/{project_id}/workflows/{project_workflow_id}/pause",
    response_model=ProjectWorkflowView,
)
async def pause_project_workflow(
    project_id: UUID,
    project_workflow_id: UUID,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> ProjectWorkflowView:
    await _require_project_access(project_id, request, user)
    return ProjectWorkflowView.model_validate(
        await _set_project_workflow_pause(
            project_id=project_id,
            project_workflow_id=project_workflow_id,
            paused=True,
            request=request,
        )
    )


@router.post(
    "/api/projects/{project_id}/workflows/{project_workflow_id}/skip-once",
    response_model=ProjectWorkflowView,
)
async def skip_project_workflow_once(
    project_id: UUID,
    project_workflow_id: UUID,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> ProjectWorkflowView:
    await _require_project_access(project_id, request, user)
    database = request.app.state.runtime.database
    configured = await database.get_project_workflow(project_workflow_id)
    if configured is None or configured.project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project workflow not found"
        )
    if (
        configured.status != "active"
        or configured.schedule is None
        or configured.next_run_at is None
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="this workflow has no next scheduled run to skip",
        )
    schedule = WorkflowSchedule.model_validate(configured.schedule)
    try:
        skipped = await database.skip_project_workflow_once(
            project_workflow_id=project_workflow_id,
            project_id=project_id,
            skipped_for=configured.next_run_at,
            next_run_at=next_run_after(schedule, configured.next_run_at),
            clerk_user_id=user.clerk_user_id,
            workflow_key=configured.workflow_key,
            workflow_title=configured.name,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return ProjectWorkflowView.model_validate(skipped)


@router.delete(
    "/api/projects/{project_id}/workflows/{project_workflow_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_project_workflow(
    project_id: UUID,
    project_workflow_id: UUID,
    request: Request,
    expected_settings_revision: int = Query(ge=1),
    user: AuthContext = AUTHENTICATED_USER,
) -> Response:
    await _require_project_access(project_id, request, user)
    database = request.app.state.runtime.database
    configured = await database.get_project_workflow(project_workflow_id, include_archived=True)
    if configured is None or configured.project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project workflow not found"
        )
    if configured.settings_revision != expected_settings_revision:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="workflow settings changed after this editor was opened",
        )
    from tin_lite.project_workflow_operations import archive_configuration

    try:
        await archive_configuration(
            runtime=request.app.state.runtime,
            settings=request.app.state.settings,
            configured=configured,
            actor=user.clerk_user_id,
            expected_settings_revision=expected_settings_revision,
        )
    except StaleSettingsRevisionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Tin could not remove this workflow schedule",
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/api/projects/{project_id}/workflows/{project_workflow_id}/resume",
    response_model=ProjectWorkflowView,
)
async def resume_project_workflow(
    project_id: UUID,
    project_workflow_id: UUID,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> ProjectWorkflowView:
    await _require_project_access(project_id, request, user)
    return ProjectWorkflowView.model_validate(
        await _set_project_workflow_pause(
            project_id=project_id,
            project_workflow_id=project_workflow_id,
            paused=False,
            request=request,
        )
    )


@router.get("/api/projects/{project_id}/runs", response_model=list[RunView])
async def list_project_runs(
    project_id: UUID,
    request: Request,
    response: Response,
    limit: int = Query(default=100, ge=1, le=200),
    user: AuthContext = AUTHENTICATED_USER,
) -> list[RunView]:
    await _require_project_access(project_id, request, user)
    runs = await request.app.state.runtime.database.list_runs(project_id=project_id, limit=limit)
    response.headers["X-Tin-Read-Source"] = "postgres"
    from tin_lite.content_delivery import ContentDelivery

    deliveries = await ContentDelivery(database=request.app.state.runtime.database).statuses(runs)
    return [
        RunView.model_validate(item).model_copy(
            update={"content_delivery": deliveries.get(item.id)}
        )
        for item in runs
    ]


@router.get("/api/projects/{project_id}/activity", response_model=list[ActivityView])
async def list_project_activity(
    project_id: UUID,
    request: Request,
    response: Response,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: AuthContext = AUTHENTICATED_USER,
) -> list[ActivityView]:
    await _require_project_access(project_id, request, user)
    events = await request.app.state.runtime.database.list_product_activity(
        project_id=project_id,
        limit=limit,
        offset=offset,
    )
    response.headers["X-Tin-Read-Source"] = "postgres"
    return [ActivityView.model_validate(item) for item in events]


@router.get("/api/workflows/{workflow_id}", response_model=WorkflowView)
async def get_workflow(
    workflow_id: UUID,
    request: Request,
    response: Response,
    project_id: UUID | None = None,
    user: AuthContext = AUTHENTICATED_USER,
) -> WorkflowView:
    workflow = await request.app.state.runtime.database.get_workflow(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workflow not found")
    if workflow.project_id is not None:
        await _require_project_access(workflow.project_id, request, user)
    view = _workflow_view(workflow, request.app.state.settings)
    if project_id is not None:
        await _require_project_access(project_id, request, user)
        readiness = await project_readiness(
            database=request.app.state.runtime.database,
            storage=getattr(request.app.state.runtime, "storage", None),
            project_id=project_id,
            workflows=[workflow],
        )
        view = view.model_copy(update={"readiness": readiness[workflow.id]})
    response.headers["X-Tin-Read-Source"] = "postgres"
    return view


@router.post(
    "/api/workflows/content.design_md/runs",
    response_model=RunView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_design_run(
    payload: RunCreate,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: AuthContext = AUTHENTICATED_USER,
) -> RunView:
    await _require_project_access(payload.project_id, request, user)
    database = request.app.state.runtime.database
    workflow = await database.get_registry_workflow(WORKFLOW_NAME)
    if workflow is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="built-in workflow is not available",
        )
    return await _start_workflow_run(
        workflow,
        payload,
        request,
        user,
        start_idempotency_key=_idempotency_key(idempotency_key),
    )


@router.post(
    "/api/workflows/{workflow_id}/runs",
    response_model=RunView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_workflow_run(
    workflow_id: UUID,
    payload: RunCreate,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: AuthContext = AUTHENTICATED_USER,
) -> RunView:
    await _require_project_access(payload.project_id, request, user)
    workflow = await request.app.state.runtime.database.get_workflow(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workflow not found")
    return await _start_workflow_run(
        workflow,
        payload,
        request,
        user,
        start_idempotency_key=_idempotency_key(idempotency_key),
    )


async def _start_workflow_run(
    workflow: Workflow,
    payload: RunCreate,
    request: Request,
    user: AuthContext,
    *,
    start_idempotency_key: str | None = None,
) -> RunView:
    inputs = dict(payload.inputs)
    legacy = {
        key: value
        for key, value in {"instruction": payload.instruction, "title": payload.title}.items()
        if value is not None
    }
    if legacy:
        if workflow.executor != PROJECT_TASK_WORKFLOW_NAME:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="instruction and title are only valid for a one-off project task",
            )
        overlap = set(inputs).intersection(legacy)
        if overlap:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"duplicate workflow input: {sorted(overlap)[0]}",
            )
        inputs.update(legacy)
    try:
        run = await dispatch_workflow_run(
            runtime=request.app.state.runtime,
            settings=request.app.state.settings,
            workflow=workflow,
            project_id=payload.project_id,
            started_by_clerk_user_id=user.clerk_user_id,
            start_idempotency_key=start_idempotency_key,
            input_payload=inputs,
            payment_card=payload.payment_card,
            billing_quote_id=payload.billing_quote_id,
            **_run_start_provenance(user),
        )
    except BillingError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.diagnostic()) from exc
    except PrerequisiteError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.diagnostic()) from exc
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except WorkflowExecutorUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except TemporalStartError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"run_id": str(exc.run_id), "message": str(exc)},
        ) from exc
    except IntegrationError as exc:
        raise _integration_http_error(exc) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return RunView.model_validate(run)


def _run_start_provenance(user: AuthContext) -> dict[str, str]:
    if user.token_type != "oauth_token":  # noqa: S105
        return {}
    provenance = {"trigger_source": "api"}
    if user.client_id is not None:
        provenance["started_by_oauth_client_id"] = user.client_id
    return provenance


async def _sync_project_workflow_schedule(
    configured: ProjectWorkflow,
    request: Request,
    *,
    previous_schedule: dict | None = None,
    paused: bool | None = None,
) -> ProjectWorkflow:
    from tin_lite.project_workflow_operations import sync_project_workflow

    database = request.app.state.runtime.database
    try:
        return await sync_project_workflow(
            runtime=request.app.state.runtime,
            settings=request.app.state.settings,
            configured=configured,
            previous_schedule=previous_schedule,
            paused=paused,
        )
    except Exception as exc:
        await database.project_workflow_failed(
            project_workflow_id=configured.id,
            error_message=f"{type(exc).__name__}: schedule synchronization failed",
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Tin could not synchronize this workflow schedule",
        ) from exc


async def _set_project_workflow_pause(
    *,
    project_id: UUID,
    project_workflow_id: UUID,
    paused: bool,
    request: Request,
) -> ProjectWorkflow:
    database = request.app.state.runtime.database
    configured = await database.get_project_workflow(project_workflow_id)
    if configured is None or configured.project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="project workflow not found"
        )
    if configured.schedule is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="only a scheduled workflow can be paused",
        )
    from tin_lite.project_workflow_operations import set_schedule_paused

    try:
        return await set_schedule_paused(
            runtime=request.app.state.runtime,
            settings=request.app.state.settings,
            configured=configured,
            paused=paused,
        )
    except (LookupError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/api/workflows/runs/{run_id}", response_model=RunView)
async def get_run(
    run_id: UUID,
    request: Request,
    response: Response,
    user: AuthContext = AUTHENTICATED_USER,
) -> RunView:
    run = await _run_from_postgres(run_id, request, user)
    response.headers["X-Tin-Read-Source"] = "postgres"
    from tin_lite.content_delivery_api import delivery_service

    return RunView.model_validate(run).model_copy(
        update={"content_delivery": await delivery_service(request.app.state.runtime).status(run)}
    )


@router.get("/api/workflows/runs/{run_id}/usage")
async def get_run_usage(
    run_id: UUID,
    request: Request,
    response: Response,
    user: AuthContext = AUTHENTICATED_USER,
) -> dict:
    from tin_lite.run_usage import read_run_usage

    run = await _run_from_postgres(run_id, request, user)
    response.headers["X-Tin-Read-Source"] = "postgres"
    response.headers["Cache-Control"] = "no-store"
    return await read_run_usage(database=request.app.state.runtime.database, run=run)


def _output_resolution_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, OutputResolutionError):
        return HTTPException(
            status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)}
        )
    if isinstance(exc, SideEffectConflictError):
        return HTTPException(
            status_code=409, detail={"code": "request_conflict", "message": str(exc)}
        )
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail="saved output not found")
    # Storage/transport errors may contain signed URLs. Never return their raw text.
    return HTTPException(
        status_code=503,
        detail={
            "code": "output_unavailable",
            "message": "Saved-output verification is unavailable. Retry the same request.",
        },
    )


@router.get("/api/workflows/runs/{run_id}/output-comparison")
async def compare_run_output(
    run_id: UUID,
    request: Request,
    response: Response,
    user: AuthContext = AUTHENTICATED_USER,
) -> dict:
    await _run_from_postgres(run_id, request, user)
    response.headers["Cache-Control"] = "no-store"
    try:
        return await request.app.state.runtime.output_resolution.compare(run_id=run_id)
    except Exception as exc:
        raise _output_resolution_http_error(exc) from exc


@router.get("/api/workflows/runs/{run_id}/output-resolution")
async def get_run_output_resolution(
    run_id: UUID,
    request: Request,
    response: Response,
    user: AuthContext = AUTHENTICATED_USER,
) -> dict:
    await _run_from_postgres(run_id, request, user)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Tin-Read-Source"] = "postgres"
    try:
        return await request.app.state.runtime.output_resolution.status(
            run_id=run_id, actor_clerk_user_id=user.clerk_user_id, client_id=user.client_id
        )
    except Exception as exc:
        raise _output_resolution_http_error(exc) from exc


@router.post("/api/workflows/runs/{run_id}/output-resolution")
async def resolve_run_output(
    run_id: UUID,
    payload: OutputResolutionRequest,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> dict:
    await _run_from_postgres(run_id, request, user)
    try:
        return await request.app.state.runtime.output_resolution.resolve(
            run_id=run_id,
            request=payload,
            actor_clerk_user_id=user.clerk_user_id,
            client_id=user.client_id,
        )
    except Exception as exc:
        raise _output_resolution_http_error(exc) from exc


@router.get(
    "/api/outreach/campaigns/{run_id}",
    response_model=OutreachCampaignView,
)
async def get_outreach_campaign(
    run_id: UUID,
    request: Request,
    response: Response,
    user: AuthContext = AUTHENTICATED_USER,
) -> OutreachCampaignView:
    await _run_from_postgres(run_id, request, user)
    campaign = await request.app.state.runtime.database.get_outreach_campaign_projection(run_id)
    if campaign is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="campaign not found")
    response.headers["X-Tin-Read-Source"] = "postgres"
    return OutreachCampaignView.model_validate(campaign)


@router.get(
    "/api/outreach/campaigns/{run_id}/deliveries",
    response_model=list[OutreachCampaignDeliveryView],
)
async def list_outreach_campaign_deliveries(
    run_id: UUID,
    request: Request,
    response: Response,
    user: AuthContext = AUTHENTICATED_USER,
) -> list[OutreachCampaignDeliveryView]:
    run = await _run_from_postgres(run_id, request, user)
    if run.workflow_name != EMAIL_CAMPAIGN_WORKFLOW_NAME:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="campaign not found")
    deliveries = await request.app.state.runtime.database.list_outreach_campaign_deliveries(
        run_id=run_id
    )
    response.headers["X-Tin-Read-Source"] = "postgres"
    return [OutreachCampaignDeliveryView.model_validate(item) for item in deliveries]


@router.post(
    "/api/outreach/campaigns/{run_id}/revisions",
    response_model=OutreachCampaignRevisionView,
    status_code=status.HTTP_201_CREATED,
)
async def revise_outreach_campaign(
    run_id: UUID,
    payload: OutreachCampaignRevisionCreate,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> OutreachCampaignRevisionView:
    run = await _run_from_postgres(run_id, request, user)
    if run.workflow_name != EMAIL_CAMPAIGN_WORKFLOW_NAME:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="run is not an email campaign"
        )
    try:
        revision = await request_email_campaign_revision(
            database=request.app.state.runtime.database,
            storage=request.app.state.runtime.storage,
            run_id=run_id,
            request_id=payload.request_id,
            follow_up_body=payload.follow_up_body,
            clerk_user_id=user.clerk_user_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (RuntimeError, SideEffectConflictError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return OutreachCampaignRevisionView.model_validate(revision)


@router.post(
    "/api/outreach/campaigns/{run_id}/revisions/{revision_id}/approve",
    response_model=RunView,
)
async def approve_outreach_campaign_revision(
    run_id: UUID,
    revision_id: UUID,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> RunView:
    run = await _run_from_postgres(run_id, request, user)
    if run.workflow_name != EMAIL_CAMPAIGN_WORKFLOW_NAME:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="run is not an email campaign"
        )
    try:
        updated = await request.app.state.runtime.database.approve_email_campaign_revision(
            run_id=run_id,
            revision_id=revision_id,
            clerk_user_id=user.clerk_user_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return RunView.model_validate(updated)


@router.post(
    "/api/outreach/campaigns/{run_id}/revisions/{revision_id}/discard",
    response_model=RunView,
)
async def discard_outreach_campaign_revision(
    run_id: UUID,
    revision_id: UUID,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> RunView:
    run = await _run_from_postgres(run_id, request, user)
    if run.workflow_name != EMAIL_CAMPAIGN_WORKFLOW_NAME:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="run is not an email campaign"
        )
    try:
        updated = await request.app.state.runtime.database.discard_email_campaign_revision(
            run_id=run_id,
            revision_id=revision_id,
            clerk_user_id=user.clerk_user_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return RunView.model_validate(updated)


@router.get("/api/tasks/{run_id}", response_model=ProjectTaskView)
async def get_project_task(
    run_id: UUID,
    request: Request,
    response: Response,
    user: AuthContext = AUTHENTICATED_USER,
) -> ProjectTaskView:
    run = await _project_task_from_postgres(run_id, request, user)
    entries = await request.app.state.runtime.database.list_task_entries(run_id=run_id)
    response.headers["X-Tin-Read-Source"] = "postgres"
    return ProjectTaskView(
        run=RunView.model_validate(run),
        entries=[TaskEntryView.model_validate(entry) for entry in entries],
    )


@router.get("/api/tasks/{run_id}/review/file")
async def get_project_task_review_file(
    run_id: UUID,
    request: Request,
    path: str = Query(min_length=1, max_length=512),
    user: AuthContext = AUTHENTICATED_USER,
) -> Response:
    run, content = await _read_project_task_review_file(run_id, path, request, user)
    filename = PurePosixPath(path).name
    return Response(
        content=content,
        media_type="text/markdown; charset=utf-8",
        headers={
            **_project_content_headers(filename, "text/markdown"),
            "X-Tin-Artifact-Source": "code.storage",
            "X-Tin-Task-Review": str(run.id),
        },
    )


@router.get(
    "/api/tasks/{run_id}/review/document",
    response_model=MarkdownDocumentView,
)
async def get_project_task_review_document(
    run_id: UUID,
    request: Request,
    response: Response,
    path: str = Query(min_length=1, max_length=512),
    user: AuthContext = AUTHENTICATED_USER,
) -> MarkdownDocumentView:
    run, content = await _read_project_task_review_file(run_id, path, request, user)
    if PurePosixPath(path).suffix.casefold() not in {".md", ".markdown"}:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="review file is not Markdown",
        )
    try:
        markdown = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Markdown review file is not UTF-8",
        ) from exc
    document = render_markdown(markdown)
    source_url = f"/api/tasks/{run.id}/review/file?path={quote(path, safe='')}"
    response.headers["X-Tin-Artifact-Source"] = "code.storage"
    response.headers["X-Tin-Task-Review"] = str(run.id)
    return MarkdownDocumentView(
        markdown=document.markdown,
        html=document.html,
        filename=PurePosixPath(path).name,
        source_url=source_url,
        timestamp=run.review_requested_at or run.created_at,
        word_count=document.word_count,
        reading_minutes=document.reading_minutes,
        headings=[
            MarkdownHeadingView(id=heading.id, title=heading.title) for heading in document.headings
        ],
    )


@router.post("/api/tasks/{run_id}/messages", response_model=ProjectTaskView)
async def send_project_task_message(
    run_id: UUID,
    payload: TaskMessageCreate,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> ProjectTaskView:
    try:
        result = await project_task_control.send_project_task_message(
            runtime=request.app.state.runtime,
            run_id=run_id,
            clerk_user_id=user.clerk_user_id,
            message=payload.message,
            request_id=payload.request_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except project_task_control.ProjectTaskConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except project_task_control.ProjectTaskDeliveryError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return ProjectTaskView(
        run=RunView.model_validate(result.run),
        entries=[TaskEntryView.model_validate(item) for item in result.entries],
    )


@router.post("/api/tasks/{run_id}/pause", response_model=RunView)
async def pause_project_task(
    run_id: UUID,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> RunView:
    run = await _project_task_from_postgres(run_id, request, user)
    if run.status == RunStatus.NEEDS_INPUT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="task is already waiting at a saved checkpoint",
        )
    if run.status == RunStatus.PAUSED:
        return RunView.model_validate(run)
    try:
        run = await request.app.state.runtime.database.request_task_control(
            run_id=run_id, control="pause"
        )
        delivered = await request.app.state.runtime.sandboxes.control_task(
            run_id=str(run_id), control={"type": "pause"}
        )
        if not delivered and run.sandbox_id is not None:
            await request.app.state.runtime.sandboxes.kill(run.sandbox_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="task pause was not accepted"
        ) from exc
    return RunView.model_validate(run)


@router.post("/api/tasks/{run_id}/resume", response_model=RunView)
async def resume_project_task(
    run_id: UUID,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> RunView:
    run = await _project_task_from_postgres(run_id, request, user)
    try:
        run = await request.app.state.runtime.database.clear_task_control(run_id=run_id)
        handle = request.app.state.runtime.temporal.get_workflow_handle(run.temporal_workflow_id)
        await handle.signal("resume")
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="task resume was not accepted"
        ) from exc
    return RunView.model_validate(run)


@router.post("/api/tasks/{run_id}/stop", response_model=RunView)
async def stop_project_task(
    run_id: UUID,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> RunView:
    run = await _project_task_from_postgres(run_id, request, user)
    if run.status == RunStatus.STOPPED:
        return RunView.model_validate(run)
    try:
        run = await request.app.state.runtime.database.request_task_control(
            run_id=run_id, control="stop"
        )
        delivered = await request.app.state.runtime.sandboxes.control_task(
            run_id=str(run_id), control={"type": "stop"}
        )
        handle = request.app.state.runtime.temporal.get_workflow_handle(run.temporal_workflow_id)
        await handle.signal("stop")
        if not delivered and run.sandbox_id is not None:
            await request.app.state.runtime.sandboxes.kill(run.sandbox_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="task stop was not accepted"
        ) from exc
    return RunView.model_validate(run)


@router.post(
    "/api/tasks/{run_id}/approve",
    response_model=RunView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def approve_project_task(
    run_id: UUID,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> RunView:
    try:
        run = await project_task_control.approve_project_task(
            runtime=request.app.state.runtime, run_id=run_id, clerk_user_id=user.clerk_user_id
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except project_task_control.ProjectTaskConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except project_task_control.ProjectTaskDeliveryError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return RunView.model_validate(run)


def _workflow_reviews(request):
    from tin_lite.workflow_reviews import WorkflowReviews

    return WorkflowReviews(runtime=request.app.state.runtime, settings=request.app.state.settings)


@router.get("/api/workflows/runs/{run_id}/review")
async def workflow_review(run_id: UUID, request: Request, user: AuthContext = AUTHENTICATED_USER):
    try:
        return await _workflow_reviews(request).view(run_id, user.clerk_user_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/api/workflows/runs/{run_id}/request-changes", response_model=RunView, status_code=202
)
async def request_workflow_changes(
    run_id: UUID,
    payload: WorkflowRevisionRequest,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
):
    from tin_lite.workflow_review_store import ReviewConflict

    try:
        run = await _workflow_reviews(request).request_changes(
            run_id=run_id,
            actor=user.clerk_user_id,
            feedback=payload.feedback,
            request_id=payload.request_id,
            token=payload.review_token,
            reference_files=payload.reference_files,
            billing_quote_id=payload.billing_quote_id,
        )
        return RunView.model_validate(run)
    except BillingError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.diagnostic()) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ReviewConflict, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/workflows/runs/{run_id}/review/compare")
async def compare_workflow_versions(
    run_id: UUID,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
):
    try:
        return await _workflow_reviews(request).compare(run_id, user.clerk_user_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/api/workflows/runs/{run_id}/review/document", response_model=MarkdownDocumentView)
async def read_previous_review_copy(
    run_id: UUID,
    request: Request,
    response: Response,
    user: AuthContext = AUTHENTICATED_USER,
):
    """Read a revision's previous copy without calling it that revision's own output."""
    try:
        service = _workflow_reviews(request)
        run, _ = await service.source(run_id, user.clerk_user_id)
        artifact_run, _ = await service.artifact(run)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    response.headers["X-Tin-Review-Copy-Run"] = str(artifact_run.id)
    return await get_artifact_document(artifact_run.id, request, response, user)


@router.post(
    "/api/workflows/runs/{run_id}/approve",
    response_model=RunView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def approve_run(
    run_id: UUID,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
    payload: WorkflowReviewApproval | None = None,
) -> RunView:
    run = await _run_from_postgres(run_id, request, user)
    from tin_lite.workflow_review_store import ReviewConflict
    from tin_lite.workflow_reviews import SUPPORTED_IDS, WorkflowReviews

    if payload is not None and payload.delivery is not None:
        # Record the pick before the approval so a refused pick never approves blindly.
        await _choose_content_delivery(run, payload, request, user)
    from tin_lite.reviewed_documents import document_spec

    if run.workflow_id in SUPPORTED_IDS or (
        run.executor == "codex.procedure"
        and await document_spec(
            request.app.state.runtime.database, request.app.state.runtime.storage, run
        )
    ):
        try:
            updated = await WorkflowReviews(
                runtime=request.app.state.runtime, settings=request.app.state.settings
            ).approve(
                run_id=run.id,
                actor=user.clerk_user_id,
                token=payload.review_token if payload else None,
            )
        except ReviewConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return RunView.model_validate(updated)
    if run.workflow_name == EMAIL_CAMPAIGN_WORKFLOW_NAME and run.status == RunStatus.NEEDS_INPUT:
        revision = await request.app.state.runtime.database.get_pending_email_campaign_revision(
            run_id=run_id
        )
        if revision is not None:
            try:
                updated = await request.app.state.runtime.database.approve_email_campaign_revision(
                    run_id=run_id,
                    revision_id=revision["id"],
                    clerk_user_id=user.clerk_user_id,
                )
            except RuntimeError as exc:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
            return RunView.model_validate(updated)
    if run.workflow_name == GROWTH_ONBOARDING_KEY and run.status == RunStatus.NEEDS_INPUT:
        try:
            await ensure_onboarding_approvable(runtime=request.app.state.runtime, run=run)
        except OnboardingPickError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if not run.review_required:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="run does not require human review",
        )
    if run.review_decision == "approved" or run.status == RunStatus.SUCCEEDED:
        return RunView.model_validate(run)
    if run.status != RunStatus.NEEDS_INPUT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="run is not waiting for human review",
        )
    try:
        await request.app.state.runtime.database.set_review_actor(
            run_id=run.id,
            clerk_user_id=user.clerk_user_id,
        )
        handle = request.app.state.runtime.temporal.get_workflow_handle(run.temporal_workflow_id)
        await handle.signal("approve")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="human review signal was not accepted",
        ) from exc
    return RunView.model_validate(run)


@router.post(
    "/api/workflows/runs/{run_id}/stop-email-campaign",
    response_model=RunView,
    status_code=status.HTTP_202_ACCEPTED,
)
async def stop_email_campaign(
    run_id: UUID,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> RunView:
    run = await _run_from_postgres(run_id, request, user)
    if run.workflow_name != EMAIL_CAMPAIGN_WORKFLOW_NAME:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="run is not an email campaign",
        )
    if run.status == RunStatus.STOPPED:
        return RunView.model_validate(run)
    try:
        stopped = await request.app.state.runtime.database.stop_email_campaign(run_id=run_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    try:
        handle = request.app.state.runtime.temporal.get_workflow_handle(
            stopped.temporal_workflow_id
        )
        await handle.cancel()
    except Exception:
        logger.warning(
            "email campaign is stopped in product state but Temporal cancellation is pending",
            extra={"run_id": str(run_id)},
        )
    return RunView.model_validate(stopped)


@router.get("/api/projects/{project_id}/brand")
async def get_project_brand(
    project_id: UUID,
    request: Request,
    revision: str | None = None,
    user: AuthContext = AUTHENTICATED_USER,
) -> dict:
    from tin_lite.brand_capture import resolve_brand

    await _require_project_access(project_id, request, user)
    runtime = request.app.state.runtime
    project = await runtime.database.get_project(project_id)
    if revision is None:
        repo = await runtime.storage.get_repo(project.state_repo_id)
        revision = await runtime.storage.head_sha(repo, project.canonical_branch)
    try:
        return await resolve_brand(runtime.storage, project, revision)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/projects/{project_id}/memory", response_model=ProjectMemoryView)
async def get_project_memory(
    project_id: UUID,
    request: Request,
    response: Response,
    user: AuthContext = AUTHENTICATED_USER,
) -> ProjectMemoryView:
    project = await _require_project_access(project_id, request, user)
    response.headers["X-Tin-Read-Source"] = "postgres"
    return ProjectMemoryView(
        project_id=project.id,
        ready=project.memory_index is not None,
        commit_sha=project.memory_commit_sha,
        path=project.memory_index_path,
        content=project.memory_index,
    )


@router.get("/api/workflows/runs/{run_id}/artifact")
async def get_artifact(
    run_id: UUID,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
    source: Literal["canonical", "retained"] = "canonical",
) -> Response:
    _run, output = await _read_run_artifact(run_id, request, user, source=source)
    filename = PurePosixPath(output.path).name
    media_type = _file_media_type(filename)
    return Response(
        content=output.content,
        media_type=media_type,
        headers={
            **_project_content_headers(filename, media_type),
            "X-Tin-Artifact-Source": "code.storage",
            "X-Tin-Output-Kind": source,
            "X-Tin-Output-Revision": output.revision,
        },
    )


@router.get(
    "/api/workflows/runs/{run_id}/artifact/document",
    response_model=MarkdownDocumentView,
)
async def get_artifact_document(
    run_id: UUID,
    request: Request,
    response: Response,
    user: AuthContext = AUTHENTICATED_USER,
    source: Literal["canonical", "retained"] = "canonical",
) -> MarkdownDocumentView:
    from tin_lite.publication import related_output_documents

    run, output = await _read_run_artifact(run_id, request, user, source=source)
    if PurePosixPath(output.path).suffix.casefold() not in {".md", ".markdown"}:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                "This output is not a Markdown document. "
                "Open it from Files or download the raw file."
            ),
        )
    try:
        markdown = output.content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Markdown artifact is not UTF-8",
        ) from exc
    document = render_markdown(markdown)
    response.headers["X-Tin-Artifact-Source"] = "code.storage"
    response.headers["X-Tin-Output-Kind"] = source
    response.headers["X-Tin-Output-Revision"] = output.revision
    return MarkdownDocumentView(
        markdown=document.markdown,
        html=document.html,
        filename=PurePosixPath(output.path).name,
        source_url=f"/api/workflows/runs/{run.id}/artifact"
        + ("?source=retained" if source == "retained" else ""),
        timestamp=run.finished_at or run.created_at,
        word_count=document.word_count,
        reading_minutes=document.reading_minutes,
        related_documents=await related_output_documents(request.app.state.runtime.database, run)
        if source == "canonical"
        else [],
        headings=[
            MarkdownHeadingView(id=heading.id, title=heading.title) for heading in document.headings
        ],
    )


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_card(
    run_id: UUID,
    request: Request,
    user: AuthContext = AUTHENTICATED_USER,
) -> HTMLResponse:
    run = await _run_from_postgres(run_id, request, user)
    artifact = (
        f'<a href="{_artifact_open_url(run)}">Open '
        f"{escape(PurePosixPath(run.artifact_path).name)}</a>"
        if run.canonical_commit_sha is not None and run.artifact_path is not None
        else "Artifact pending"
    )
    if run.canonical_commit_sha is None and run.retained_output is not None:
        path = str(run.retained_output["artifact_path"])
        output_url = (
            f"/documents/runs/{run.id}?source=retained"
            if PurePosixPath(path).suffix.casefold() in {".md", ".markdown"}
            else f"/api/workflows/runs/{run.id}/artifact?source=retained"
        )
        artifact = f'<a href="{output_url}">View generated result</a>'
    error = f"<p class=error>{escape(run.error_message)}</p>" if run.error_message else ""
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Tin Lite run</title><style>
body{{font:16px/1.5 ui-sans-serif,system-ui;background:#f4f1e8;color:#1c241d;
margin:0;padding:3rem}}
main{{max-width:42rem;margin:auto;background:#fffdf7;border:1px solid #c9c5b8;
border-radius:18px;padding:2rem}}
.status{{display:inline-block;padding:.25rem .65rem;border-radius:999px;background:#dfeadd}}
.error{{color:#8e2e25}} code{{overflow-wrap:anywhere}}
</style></head><body><main><p>Tin Lite · {escape(run.workflow_name)}</p>
<h1>{escape(run.workflow_name)}</h1><p class="status">{escape(run.status.value)}</p>
<p><code>{run.id}</code></p><p>{artifact}</p>{error}</main></body></html>"""
    return HTMLResponse(html, headers={"X-Tin-Read-Source": "postgres"})


async def _require_project_access(
    project_id: UUID,
    request: Request,
    user: AuthContext,
) -> Project:
    database = request.app.state.runtime.database
    allowed = await database.has_project_access(
        project_id=project_id,
        clerk_user_id=user.clerk_user_id,
    )
    if not allowed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    project = await database.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    return project


async def _choose_content_delivery(
    run: WorkflowRun, payload: WorkflowReviewApproval, request: Request, user: AuthContext
) -> None:
    from tin_lite.content_delivery import CHOICE_WORKFLOW_IDS
    from tin_lite.content_delivery_api import delivery_service
    from tin_lite.project_files import ProjectFileError

    if run.workflow_id not in CHOICE_WORKFLOW_IDS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This run does not publish to a repository.",
        )
    try:
        await delivery_service(request.app.state.runtime).choose(
            run=run,
            mode=payload.delivery,
            remember=payload.remember,
            actor=user.clerk_user_id,
        )
    except (LookupError, ValueError, ProjectFileError, IntegrationError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


def _integration_view(
    definition: IntegrationDefinition,
    connection: IntegrationConnection | None,
    *,
    configured: bool,
) -> IntegrationView:
    return IntegrationView(
        key=definition.key,
        name=definition.name,
        badge=definition.badge,
        description=definition.description,
        access_label=definition.access_label,
        capabilities=list(definition.capabilities),
        unlocks=list(definition.unlocks),
        setup_url=getattr(definition, "setup_url", None),
        configured=configured,
        connection_id=getattr(connection, "id", None),
        project_id=getattr(connection, "project_id", None),
        status=getattr(connection, "status", "available"),
        external_account_label=getattr(connection, "external_account_label", None),
        configuration=getattr(connection, "configuration", {}),
        connected_at=getattr(connection, "created_at", None),
        last_checked_at=getattr(connection, "last_checked_at", None),
        last_error_code=getattr(connection, "last_error_code", None),
    )


def _integration_http_error(exc: IntegrationError) -> HTTPException:
    if isinstance(exc, IntegrationNotConfiguredError):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )
    if isinstance(exc, IntegrationInputError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, ServiceCallRefused):
        # The provider answered and refused; the message is Tin's own.
        code = 429 if exc.code == "rate_limited" else status.HTTP_409_CONFLICT
        return HTTPException(status_code=code, detail=str(exc))
    if isinstance(exc, IntegrationAuthorizationError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, IntegrationUpstreamError):
        return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


async def _run_from_postgres(
    run_id: UUID,
    request: Request,
    user: AuthContext,
) -> WorkflowRun:
    run = await request.app.state.runtime.database.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    await _require_project_access(run.project_id, request, user)
    return run


async def _project_task_from_postgres(
    run_id: UUID,
    request: Request,
    user: AuthContext,
) -> WorkflowRun:
    run = await _run_from_postgres(run_id, request, user)
    if run.executor != PROJECT_TASK_WORKFLOW_NAME:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    return run


async def _read_project_file(
    *,
    project_id: UUID,
    path: str,
    revision: str,
    request: Request,
    user: AuthContext,
) -> tuple[Project, bytes]:
    project = await _require_project_access(project_id, request, user)
    if not safe_project_file_path(path):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="unsafe path")
    try:
        storage = request.app.state.runtime.storage
        if hasattr(storage, "list_canonical_files_at"):
            canonical_paths = await storage.list_canonical_files_at(
                repo_id=project.state_repo_id,
                revision=revision,
            )
        else:
            canonical_paths, canonical_revision = await storage.list_canonical_files(
                repo_id=project.state_repo_id,
                branch=project.canonical_branch,
            )
            if revision != canonical_revision:
                canonical_paths = []
        if path not in canonical_paths:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="project file not found at this revision",
            )
        content = await storage.read_canonical_artifact(
            repo_id=project.state_repo_id,
            commit_sha=revision,
            path=path,
        )
    except HTTPException:
        raise
    except Exception as exc:
        storage_status = getattr(exc, "status_code", None)
        response_status = getattr(getattr(exc, "response", None), "status_code", None)
        if storage_status == 404 or response_status == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="project file not found at this revision",
            ) from exc
        raise
    return project, content


async def _read_project_task_review_file(
    run_id: UUID,
    path: str,
    request: Request,
    user: AuthContext,
) -> tuple[WorkflowRun, bytes]:
    run = await _project_task_from_postgres(run_id, request, user)
    if PurePosixPath(path).suffix.casefold() not in {".md", ".markdown"}:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="review file is not Markdown",
        )
    if run.task_diff is None or run.ephemeral_branch is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="task review is not ready",
        )
    files = run.task_diff.get("files")
    if not isinstance(files, list):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="task review is not ready",
        )
    reviewed_file = next(
        (
            item
            for item in files
            if isinstance(item, dict)
            and item.get("path") == path
            and item.get("state") != "deleted"
        ),
        None,
    )
    if reviewed_file is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="review file not found")
    project = await request.app.state.runtime.database.get_project(run.project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    if run.status == RunStatus.SUCCEEDED and run.canonical_commit_sha is not None:
        content = await request.app.state.runtime.storage.read_canonical_artifact(
            repo_id=project.state_repo_id,
            commit_sha=run.canonical_commit_sha,
            path=path,
        )
    else:
        content = await request.app.state.runtime.storage.read_ephemeral_artifact(
            repo_id=project.state_repo_id,
            branch=run.ephemeral_branch,
            path=path,
        )
    return run, content


async def _read_run_artifact(
    run_id: UUID,
    request: Request,
    user: AuthContext,
    *,
    source: Literal["canonical", "retained"] = "canonical",
) -> tuple[WorkflowRun, RunOutput]:
    run = await _run_from_postgres(run_id, request, user)
    project = await request.app.state.runtime.database.get_project(run.project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="project not found")
    try:
        output = await read_run_output(
            storage=request.app.state.runtime.storage,
            run=run,
            repo_id=project.state_repo_id,
            source=source,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="output is not available"
        ) from exc
    return run, output


def _artifact_open_url(run: WorkflowRun) -> str:
    if run.artifact_path and PurePosixPath(run.artifact_path).suffix.casefold() in {
        ".md",
        ".markdown",
    }:
        return f"/documents/runs/{run.id}"
    return f"/api/workflows/runs/{run.id}/artifact"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
