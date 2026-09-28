from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from tin_lite import analytics
from tin_lite.api import router
from tin_lite.auth import ClerkAuth
from tin_lite.billing_api import router as billing_router
from tin_lite.billing_recovery import billing_reconciliation_loop
from tin_lite.mcp_oauth import install_shell_probe_hint
from tin_lite.mcp_oauth import router as mcp_oauth_router
from tin_lite.mcp_server import create_mcp_app
from tin_lite.run_tools import create_run_tools_app
from tin_lite.runtime import build_runtime
from tin_lite.settings import Settings, get_settings
from tin_lite.sms import router as sms_router

STATIC_DIR = Path(__file__).with_name("static")


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved_settings = settings or get_settings()
    auth = ClerkAuth(resolved_settings)
    app_ref: dict[str, FastAPI] = {}
    mcp_server, mcp_app = create_mcp_app(
        settings=resolved_settings,
        auth=auth,
        runtime=lambda: app_ref["app"].state.runtime,
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
        worker_task = asyncio.create_task(runtime.worker.run(), name="temporal-worker")
        from tin_lite.workflow_review_dispatch import review_reconciliation_loop

        review_task = asyncio.create_task(
            review_reconciliation_loop(runtime, resolved_settings), name="review-reconciliation"
        )
        billing_task = (
            asyncio.create_task(
                billing_reconciliation_loop(runtime, resolved_settings),
                name="billing-reconciliation",
            )
            if runtime.database.billing is not None
            else None
        )
        try:
            async with (
                mcp_app.router.lifespan_context(mcp_app),
                run_tools_app.router.lifespan_context(run_tools_app),
            ):
                yield
        finally:
            review_task.cancel()
            await asyncio.gather(review_task, return_exceptions=True)
            if billing_task is not None:
                billing_task.cancel()
                await asyncio.gather(billing_task, return_exceptions=True)
            # Usually already drained by the serve command's SIGTERM handling; the
            # group's shutdown is idempotent and then returns at once.
            await runtime.worker.shutdown()
            posthog_client = getattr(mcp_server, "posthog_client", None)
            if posthog_client is not None:
                posthog_client.shutdown()  # flush PostHog's MCP analytics queue
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
            await analytics.aclose()

    app = FastAPI(title="Tin Lite Switchboard", version="0.1.0", lifespan=lifespan)
    app_ref["app"] = app
    app.state.auth = auth
    app.state.mcp_server = mcp_server
    app.state.run_tools_server = run_tools_server
    app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")
    app.include_router(router)
    app.include_router(mcp_oauth_router)
    install_shell_probe_hint(app, resolved_settings)
    if resolved_settings.billing_enabled:
        app.include_router(billing_router)
    app.include_router(sms_router)
    app.mount("/internal/run-tools", run_tools_app)
    app.mount("/", mcp_app)
    return app


app = create_app()
