from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from tin_lite import public_catalog_api
from tin_lite.api import router
from tin_lite.domain import Workflow, WorkflowStatus


def workflow(key: str, **overrides) -> Workflow:
    values = {
        "id": uuid4(),
        "project_id": None,
        "key": key,
        "title": f"Title of {key}",
        "description": f"What {key} does.",
        "executor": "codex.procedure",
        "definition_repo_id": "registry/workflows",
        "definition_path": f"workflows/{key}.json",
        "current_commit_sha": "d" * 40,
        "version_label": "1.0.0",
        "definition": {"input_schema": {"secret_default": "do-not-leak"}},
        "status": WorkflowStatus.ACTIVE,
        "system_id": "organic-traffic",
        "system_name": "Organic traffic system",
        "system_order": 1,
    }
    values.update(overrides)
    return Workflow(**values)


class RegistryDatabase:
    def __init__(self, workflows: list[Workflow]) -> None:
        self.workflows = workflows
        self.reads = 0

    async def list_workflows(self, *, project_id=None):
        assert project_id is None, "the public catalog must only read the Registry"
        self.reads += 1
        return self.workflows


def client_for(database: RegistryDatabase) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(router)
    app.state.runtime = SimpleNamespace(database=database)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_lists_active_registry_workflows_without_sign_in_or_internals() -> None:
    database = RegistryDatabase(
        [
            workflow("organic.audit"),
            workflow(
                "growth.onboarding",
                definition={"agent_only": True},
                system_id="start-here",
                system_name="Start here",
                system_order=0,
            ),
            workflow("brand.capture", system_id=None, system_name=None, system_order=None),
            workflow("organic.paused", status=WorkflowStatus.PAUSED),
            workflow("custom.private", project_id=uuid4()),
        ]
    )
    async with client_for(database) as client:
        response = await client.get("/api/public/workflows")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 3
    assert [item["key"] for item in body["workflows"]] == [
        "organic.audit",
        "growth.onboarding",
        "brand.capture",
    ]
    assert body["workflows"][1]["agent_only"] is True
    assert body["workflows"][2] == {
        "key": "brand.capture",
        "title": "Title of brand.capture",
        "description": "What brand.capture does.",
        "system": None,
        "agent_only": False,
        "source": "community",
    }
    assert body["workflows"][0]["source"] == "built-in"
    assert [s["id"] for s in body["systems"]] == ["start-here", "organic-traffic"]
    for leaked in ("do-not-leak", "d" * 40, "registry/workflows", "codex.procedure"):
        assert leaked not in response.text
    assert response.headers["cache-control"].startswith("public")


@pytest.mark.asyncio
async def test_reads_the_registry_once_per_cache_window_and_honours_etags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1000.0]
    monkeypatch.setattr(public_catalog_api.time, "monotonic", lambda: now[0])
    database = RegistryDatabase([workflow("organic.audit")])
    async with client_for(database) as client:
        first = await client.get("/api/public/workflows")
        second = await client.get("/api/public/workflows")
        assert database.reads == 1
        assert second.content == first.content

        etag = first.headers["etag"]
        unchanged = await client.get("/api/public/workflows", headers={"If-None-Match": etag})
        assert unchanged.status_code == 304
        assert unchanged.content == b""

        # After the window a new read happens; the same Registry keeps the same ETag.
        now[0] += public_catalog_api.CACHE_SECONDS + 1
        refreshed = await client.get("/api/public/workflows", headers={"If-None-Match": etag})
        assert database.reads == 2
        assert refreshed.status_code == 304

        database.workflows = [workflow("organic.audit"), workflow("content.plan")]
        now[0] += public_catalog_api.CACHE_SECONDS + 1
        changed = await client.get("/api/public/workflows", headers={"If-None-Match": etag})
        assert changed.status_code == 200
        assert changed.json()["count"] == 2
        assert changed.headers["etag"] != etag
