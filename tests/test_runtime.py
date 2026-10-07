from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from temporalio import activity

from tin_lite import runtime
from tin_lite.activity_lanes import CODEX_ACTIVITIES, COLLECTION_ACTIVITIES, TRUSTED_ACTIVITIES


@pytest.mark.asyncio
@pytest.mark.parametrize("billing_enabled", [False, True])
async def test_build_runtime_wires_services_without_unknown_activity_arguments(
    monkeypatch: pytest.MonkeyPatch,
    billing_enabled: bool,
) -> None:
    database = SimpleNamespace(
        connect=AsyncMock(),
        close=AsyncMock(),
        pool=SimpleNamespace(fetchval=AsyncMock(return_value=False)),
    )
    storage = object()
    sandboxes = object()
    temporal = object()
    model_router = object()
    integrations = SimpleNamespace(x=object(), _cipher=object())
    project_files = object()
    worker = object()

    monkeypatch.setattr(runtime, "Database", lambda _dsn: database)
    monkeypatch.setattr(runtime, "CodeStorage", lambda **_kwargs: storage)
    monkeypatch.setattr(runtime, "sync_system_wiki", AsyncMock(return_value=object()))
    monkeypatch.setattr(runtime, "sync_builtin_workflows", AsyncMock())
    monkeypatch.setattr(runtime, "E2BRuntime", lambda **_kwargs: sandboxes)
    monkeypatch.setattr(runtime, "configured_model_router", lambda *_args, **_kwargs: model_router)
    monkeypatch.setattr(runtime, "IntegrationService", lambda **_kwargs: integrations)
    monkeypatch.setattr(runtime, "ProjectFileService", lambda **_kwargs: project_files)
    monkeypatch.setattr(runtime.Client, "connect", AsyncMock(return_value=temporal))
    registrations = []

    def register(*args, **kwargs):
        registrations.append(kwargs)
        return worker

    monkeypatch.setattr(runtime, "Worker", register)

    secret = SimpleNamespace(get_secret_value=lambda: "secret")
    settings = SimpleNamespace(
        billing_enabled=billing_enabled,
        runtime_dsn="postgresql://runtime",
        code_storage_org="tin",
        code_storage_api_key=secret,
        e2b_api_key=secret,
        e2b_template="tin-lite-codex",
        e2b_browser_template="tin-lite-codex-browser",
        e2b_browser_api_template="tin-lite-codex-browser-api",
        e2b_studio_template="tin-lite-codex-studio",
        e2b_studio_api_template="tin-lite-codex-studio-api",
        e2b_isolated_template=None,
        fal_key=None,
        studio_max_voice_lines_per_run=24,
        studio_max_voice_characters_per_run=3000,
        sandbox_timeout_seconds=900,
        egress_allow_hosts=(),
        proxy_grant_dir=None,
        luna_api_key=None,
        temporal_endpoint="temporal.example",
        temporal_namespace="tin-lite-dev",
        temporal_api_key=secret,
        task_queue="tin-lite-checkpoint-a",
        worker_graceful_shutdown_seconds=300,
        switchboard_public_url="https://lite.tin.computer",
    )

    services = await runtime.build_runtime(settings)

    assert services.project_files is project_files
    assert services.worker.workers == (worker, worker, worker)
    collection, original, trusted = registrations
    assert collection["task_queue"] == settings.task_queue + "-connections"
    assert collection["max_concurrent_activities"] == 2
    assert collection["graceful_shutdown_timeout"] == timedelta(minutes=5)
    assert not collection.get("workflows")
    assert all(fn.__self__.cipher is integrations._cipher for fn in collection["activities"])
    assert original["task_queue"] == settings.task_queue
    assert original["max_concurrent_activities"] == 4
    assert original["interceptors"]
    assert trusted["task_queue"] == settings.task_queue + "-trusted"
    assert trusted["max_concurrent_activities"] == 4
    # A deploy lets in-flight activities finish instead of cancelling them at once.
    assert original["graceful_shutdown_timeout"] == timedelta(minutes=5)
    assert trusted["graceful_shutdown_timeout"] == timedelta(minutes=5)
    assert not trusted.get("workflows")
    original_names = {
        activity._Definition.must_from_callable(fn).name for fn in original["activities"]
    }
    trusted_names = {
        activity._Definition.must_from_callable(fn).name for fn in trusted["activities"]
    }
    collection_names = {
        activity._Definition.must_from_callable(fn).name for fn in collection["activities"]
    }
    assert collection_names == COLLECTION_ACTIVITIES
    assert not collection_names & (original_names | trusted_names)
    assert original_names == CODEX_ACTIVITIES | TRUSTED_ACTIVITIES
    assert trusted_names == TRUSTED_ACTIVITIES
    assert not trusted_names & CODEX_ACTIVITIES
    assert (services.database.billing is not None) is billing_enabled
