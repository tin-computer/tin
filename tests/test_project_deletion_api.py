"""Project deletion over HTTP and MCP: the same contract, errors and replay on both."""

from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from test_private_workflows import app, mcp, structured
from test_procedure_publication import publication_db as publication_db
from test_project_deletion import ACTOR, OTHER, STRANGER, fixture, seed_work

from tin_lite.project_deletion import receipt_key
from tin_lite.projects import personal_project_id


async def surface_fixture(db, **kwargs):
    f = await fixture(db, **kwargs)
    f.settings = SimpleNamespace(
        switchboard_public_url="https://tin.test",
        clerk_frontend_api_url="https://clerk.test",
        task_queue="test",
        billing_enabled=False,
        private_workflow_projects=set(),
    )
    f.runtime.settings = f.settings
    return f


def client(f, actor=ACTOR):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app(f, actor)), base_url="http://t")


async def call_delete(
    surface, f, monkeypatch, *, actor=ACTOR, project_id=None, name="Acme", request_id=None
):
    """Return (ok, payload) where payload is the result dict or the error text."""
    project_id = project_id or f.project.id
    request_id = request_id or uuid4()
    if surface == "http":
        async with client(f, actor) as http:
            response = await http.delete(
                f"/api/projects/{project_id}",
                params={"request_id": str(request_id), "confirm_name": name},
            )
        if response.status_code == 200:
            return True, response.json()
        return False, f"{response.status_code} {response.json()['detail']}"
    server = mcp(f, monkeypatch, actor)
    try:
        result = structured(
            await server.call_tool(
                "delete_project",
                {
                    "project_id": str(project_id),
                    "confirm_name": name,
                    "request_id": str(request_id),
                },
            )
        )
    except ToolError as exc:
        return False, str(exc)
    return True, result


async def listed(surface, f, monkeypatch, actor=ACTOR):
    if surface == "http":
        async with client(f, actor) as http:
            return (await http.get("/api/projects")).json()
    return structured(await mcp(f, monkeypatch, actor).call_tool("list_projects", {}))["result"]


@pytest.mark.parametrize("surface", ["http", "mcp"])
async def test_the_creator_deletes_and_the_project_is_gone_from_both_surfaces(
    publication_db, monkeypatch, surface
):
    f = await surface_fixture(publication_db)
    await seed_work(f)
    before = await listed(surface, f, monkeypatch)
    assert [(p["name"], p["can_delete"]) for p in before] == [("Acme", True)]
    assert [p["can_delete"] for p in await listed(surface, f, monkeypatch, OTHER)] == [False]

    request_id = uuid4()
    ok, result = await call_delete(surface, f, monkeypatch, request_id=request_id)
    assert ok, result
    assert result["name"] == "Acme"
    assert result["stopped_runs"] == 1 and result["removed_schedules"] == 1
    if surface == "mcp":
        assert result["tell_the_founder"] == (
            "Acme is deleted. I stopped 1 running run, removed 1 schedule and disconnected "
            "1 integration; its files are gone. Billing history stays."
        )
        # A status the agent says in its own words: relay, no quote.
        assert result["relay"] == [result["tell_the_founder"]] and "quote" not in result
    assert await listed(surface, f, monkeypatch) == []
    assert await listed(surface, f, monkeypatch, OTHER) == []

    # Replay with the same request_id, and a fresh request on the deleted project.
    ok, again = await call_delete(surface, f, monkeypatch, request_id=request_id)
    assert ok and again["deleted_at"] == result["deleted_at"]
    ok, fresh = await call_delete(surface, f, monkeypatch)
    assert ok and fresh["stopped_runs"] == 0
    if surface == "mcp":
        assert (
            fresh["tell_the_founder"]
            == "Acme is deleted. Its files are gone. Billing history stays."
        )

    # Every other route and tool now treats the project as unknown.
    if surface == "http":
        async with client(f) as http:
            assert (await http.get(f"/api/projects/{f.project.id}/system")).status_code == 404
    else:
        with pytest.raises(ToolError, match="not_found"):
            await mcp(f, monkeypatch).call_tool(
                "list_project_workflows", {"project_id": str(f.project.id)}
            )


@pytest.mark.parametrize("surface", ["http", "mcp"])
async def test_refusals_match_between_surfaces(publication_db, monkeypatch, surface):
    f = await surface_fixture(publication_db)
    not_found = "404 project not found" if surface == "http" else "not_found"

    ok, error = await call_delete(surface, f, monkeypatch, actor=OTHER)
    assert not ok and not_found in error
    ok, error = await call_delete(surface, f, monkeypatch, actor=STRANGER)
    assert not ok and not_found in error
    ok, error = await call_delete(surface, f, monkeypatch, project_id=uuid4())
    assert not ok and not_found in error

    ok, error = await call_delete(surface, f, monkeypatch, name="Acme Inc")
    assert not ok
    assert ("409 " if surface == "http" else "conflict:") in error
    assert "confirm_name does not match" in error
    assert [p["name"] for p in await listed(surface, f, monkeypatch)] == ["Acme"]

    # A request_id already used by another operation.
    request_id = uuid4()
    key = receipt_key(f.project.id, request_id)
    async with f.db.effect_lock(key, "something_else") as (conn, _):
        await f.db.start_effect(conn, execution_key=key, operation="something_else")
    ok, error = await call_delete(surface, f, monkeypatch, request_id=request_id)
    assert not ok
    assert ("409 " if surface == "http" else "request_conflict:") in error
    assert (await f.db.get_project(f.project.id)).deleted_at is None

    # Older rows without a creator: any member may delete.
    g = await surface_fixture(publication_db, name="Legacy", creator=None)
    ok, result = await call_delete(surface, g, monkeypatch, actor=OTHER, name="Legacy")
    assert ok and result["name"] == "Legacy"


@pytest.mark.parametrize("surface", ["http", "mcp"])
async def test_the_personal_project_cannot_be_deleted(publication_db, monkeypatch, surface):
    f = await surface_fixture(publication_db, name="QA’s project")
    assert [(p["name"], p["can_delete"]) for p in await listed(surface, f, monkeypatch)] == [
        ("QA’s project", False)
    ]
    ok, error = await call_delete(surface, f, monkeypatch, name="QA’s project")
    assert not ok
    assert ("400 " if surface == "http" else "invalid:") in error
    assert "personal project" in error
    # A stranger learns nothing about it, not even that it is personal.
    ok, error = await call_delete(surface, f, monkeypatch, actor=STRANGER, name="QA’s project")
    assert not ok and ("404" in error or "not_found" in error)

    g = await surface_fixture(publication_db, name="Acme", project_id=personal_project_id(ACTOR))
    ok, error = await call_delete(surface, g, monkeypatch)
    assert not ok and "personal project" in error
    assert "Acme" in [p["name"] for p in await listed(surface, g, monkeypatch)]
