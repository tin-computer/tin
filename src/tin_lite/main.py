from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.routing import Route

from tin_lite import analytics
from tin_lite.api import router
from tin_lite.auth import ClerkAuth
from tin_lite.billing_api import router as billing_router
from tin_lite.billing_recovery import billing_reconciliation_loop
from tin_lite.mcp_oauth import install_shell_probe_hint
from tin_lite.mcp_oauth import router as mcp_oauth_router
from tin_lite.mcp_server import create_mcp_app
from tin_lite.rb2b_api import router as rb2b_router
from tin_lite.run_tools import create_run_tools_app
from tin_lite.runtime import build_runtime
from tin_lite.settings import Settings, get_settings
from tin_lite.sms import router as sms_router

STATIC_DIR = Path(__file__).with_name("static")


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    auth = ClerkAuth(resolved_settings)
    plugin_auth = ClerkAuth(
        resolved_settings, oauth_resource_path="/mcp/plugins", allow_legacy_clients=False
    )
    app_ref: dict[str, FastAPI] = {}
    mcp_server, mcp_app = create_mcp_app(
        settings=resolved_settings,
        auth=auth,
        runtime=lambda: app_ref["app"].state.runtime,
    )
    plugin_server, plugin_app = create_mcp_app(
        settings=resolved_settings,
        auth=plugin_auth,
        runtime=lambda: app_ref["app"].state.runtime,
        public_plugin=True,
    )
    run_tools_server, run_tools_app = create_run_tools_app(
        settings=resolved_settings,
        runtime=lambda: app_ref["app"].state.runtime,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        analytics.configure(
            resolved_settings.posthog_api_key, resolved_settings.posthog_host, source="switchboard"
        )
        runtime = await build_runtime(resolved_settings)
        app.state.settings = resolved_settings
        app.state.runtime = runtime
        # The worker and the background loops run in the main process only; a web-only
        # process (the webhook intake) serves HTTP alone.
        background: list[asyncio.Task] = []
        worker_task = None
        if not getattr(resolved_settings, "web_only", False):
            worker_task = asyncio.create_task(runtime.worker.run(), name="temporal-worker")
            from tin_lite.agent_traffic_connection import removal_loop
            from tin_lite.connection_collection_connection import session_cleanup_loop
            from tin_lite.rb2b_api import waiting_visits_loop
            from tin_lite.rb2b_slack import digest_loop
            from tin_lite.summary_posts import summary_posts_loop
            from tin_lite.workflow_review_dispatch import review_reconciliation_loop

            loops = {
                "review-reconciliation": review_reconciliation_loop(runtime, resolved_settings),
                "connection-session-cleanup": session_cleanup_loop(runtime.database),
                "analytics-removals": removal_loop(runtime.integrations),
                "run-summary-posts": summary_posts_loop(runtime, resolved_settings),
                "rb2b-digests": digest_loop(runtime, resolved_settings),
                "rb2b-waiting-visits": waiting_visits_loop(runtime, resolved_settings),
            }
            if runtime.database.billing is not None:
                loops["billing-reconciliation"] = billing_reconciliation_loop(
                    runtime, resolved_settings
                )
            background = [asyncio.create_task(loop, name=name) for name, loop in loops.items()]
        try:
            async with (
                mcp_app.router.lifespan_context(mcp_app),
                plugin_app.router.lifespan_context(plugin_app),
                run_tools_app.router.lifespan_context(run_tools_app),
            ):
                yield
        finally:
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            # Usually already drained by the serve command's SIGTERM handling; the
            # group's shutdown is idempotent and then returns at once.
            if worker_task is not None:
                await runtime.worker.shutdown()
            posthog_client = getattr(mcp_server, "posthog_client", None)
            if posthog_client is not None:
                posthog_client.shutdown()  # flush PostHog's MCP analytics queue
            if worker_task is not None:
                await worker_task
            if runtime.luna is not None:
                await runtime.luna.close()
            await runtime.integrations.close()
            await runtime.storage.close()
            await runtime.studio.close()
            await runtime.model_router.close()
            if runtime.codex_api is not None:
                await runtime.codex_api.close()
            await runtime.database.close()
            await auth.close()
            await plugin_auth.close()
            await analytics.aclose()

    app = FastAPI(title="Tin Lite Switchboard", version="0.1.0", lifespan=lifespan)
    app_ref["app"] = app
    app.state.auth = auth
    app.state.mcp_server = mcp_server
    app.state.plugin_mcp_server = plugin_server
    app.state.run_tools_server = run_tools_server
    app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")
    app.include_router(router)
    app.include_router(mcp_oauth_router)
    install_shell_probe_hint(app, resolved_settings)
    if resolved_settings.billing_enabled:
        app.include_router(billing_router)
    app.include_router(sms_router)
    app.include_router(rb2b_router)
    app.mount("/internal/run-tools", run_tools_app)
    # Exact routes retain the original path and the SDK's auth/CORS middleware.
    # They must precede the existing root mount, without intercepting /mcp/consent.
    app.router.routes.extend(
        [
            Route("/mcp/plugins", plugin_app, methods=["GET", "POST", "DELETE", "OPTIONS"]),
            Route(
                "/.well-known/oauth-protected-resource/mcp/plugins",
                plugin_app,
                methods=["GET", "OPTIONS"],
            ),
        ]
    )
    app.mount("/", mcp_app)
    return app


app = create_app()
