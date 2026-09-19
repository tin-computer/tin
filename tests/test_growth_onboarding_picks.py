"""Onboarding picks are recorded structurally and approval waits for them, on both surfaces."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from mcp.server.mcpserver.exceptions import ToolError
from test_growth_onboarding import PLAN, UNTICKED
from test_procedure_publication import activity_fixture
from test_procedure_publication import publication_db as publication_db
from test_service_billing import install

from tin_lite import growth_onboarding
from tin_lite.api import router
from tin_lite.auth import AuthContext, require_user
from tin_lite.growth_onboarding import PLAN_PATH, plan_readiness
from tin_lite.mcp_server import _run_allowed_actions, create_mcp_app
from tin_lite.project_files import ProjectFileService

MEMBER = "user_member"
OUTSIDER = "user_outsider"
CONNECTED = [SimpleNamespace(provider_key="infra.github", status="connected")]
PICKS = [
    {"provider": "infra.github", "decision": "connected"},
    {
        "provider": "workspace.google",
        "decision": "not_now",
        "reason": "personal inbox stays private",
    },
]


async def harness(db, monkeypatch, *, plan=UNTICKED, executor=growth_onboarding.KEY, hold=True):
    _, storage, run, _ = await activity_fixture(db, review=True)
    for key in (
        "visibility.audit",
        "organic.audit",
        "outreach.email_shortlist",
        "site.health_improve",
        "project.weekly_brief",
        "content.public_article",
    ):
        await install(SimpleNamespace(db=db), key)
    storage.repo.edit({PLAN_PATH: plan.encode()}, "growth.onboarding_plan")

    async def read(*, repo_id, commit_sha, path):
        return storage.repo.trees[commit_sha][path][1]

    async def commit(*, repo_id, branch, expected_head_sha, request_id, message, changes):
        if storage.repo.head != expected_head_sha:
            raise RuntimeError("canonical project state changed before file commit")
        sha = storage.repo.edit({c.path: c.content.encode() for c in changes}, message)
        return sha, tuple(c.path for c in changes)

    monkeypatch.setattr(storage, "read_canonical_artifact", read)
    monkeypatch.setattr(storage, "commit_project_changes", commit)
    await db.pool.execute("UPDATE workflow_runs SET executor=$2 WHERE id=$1", run.id, executor)
    if hold:
        await db.request_human_review(
            run_id=run.id,
            canonical_commit_sha=storage.repo.head,
            artifact_ref=f"code.storage://{storage.repo.id}@{storage.repo.head}/{PLAN_PATH}",
            artifact_path=PLAN_PATH,
            summary="Pick an option.",
        )
    monkeypatch.setattr(
        db, "has_project_access", AsyncMock(side_effect=lambda **kw: kw["clerk_user_id"] == MEMBER)
    )
    monkeypatch.setattr(db, "record_mcp_usage", AsyncMock())
    monkeypatch.setattr(db, "record_tin_user", AsyncMock())
    handle = SimpleNamespace(signal=AsyncMock())
    runtime = SimpleNamespace(
        database=db,
        storage=storage,
        project_files=ProjectFileService(database=db, storage=storage),
        integrations=SimpleNamespace(list_connections=AsyncMock(return_value=list(CONNECTED))),
        temporal=SimpleNamespace(get_workflow_handle=Mock(return_value=handle)),
    )
    token = SimpleNamespace(subject=MEMBER, scopes=["openid"], client_id="client_test")
    monkeypatch.setattr("tin_lite.mcp_server.get_access_token", lambda: token)
    server, _ = create_mcp_app(
        settings=SimpleNamespace(
            switchboard_public_url="https://tin.test", clerk_frontend_api_url="https://clerk.test"
        ),
        auth=SimpleNamespace(),
        runtime=lambda: runtime,
    )
    app = FastAPI()
    app.include_router(router)
    app.state.runtime = runtime
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id=token.subject,
        token_type="session_token",  # noqa: S106
    )
    return SimpleNamespace(
        db=db,
        storage=storage,
        run=await db.get_run(run.id),
        server=server,
        app=app,
        handle=handle,
        token=token,
    )


def picks_args(h, **overrides):
    return {
        "run_id": str(h.run.id),
        "request_id": str(uuid4()),
        "systems": ["ai-visibility"],
        "control": "review_in_tin",
        "connections": PICKS,
        **overrides,
    }


async def call(h, tool, **arguments):
    return (await h.server.call_tool(tool, arguments)).structured_content


async def refused(h, tool, **arguments) -> str:
    with pytest.raises(ToolError) as caught:
        await h.server.call_tool(tool, arguments)
    return str(caught.value)


def plan_at_head(h) -> str:
    return h.storage.repo.trees[h.storage.repo.head][PLAN_PATH][1].decode()


async def test_picks_are_ticked_into_the_plan_and_replay_safely(publication_db, monkeypatch):
    h = await harness(publication_db, monkeypatch)
    before = h.storage.repo.head
    assert h.run.status.value == "needs_input"
    assert _run_allowed_actions(h.run) == ["record_picks", "approve"]

    args = picks_args(h)
    result = await call(h, "record_onboarding_picks", **args)
    assert result["revision"] == h.storage.repo.head != before
    assert result["replayed"] is False
    assert result["systems"] == ["ai-visibility"] and result["control"] == "review_in_tin"
    assert result["connections"]["workspace.google"] == {
        "state": "declined",
        "note": "personal inbox stays private",
    }
    assert result["next"] == "approve_workflow_run"
    assert "/decisions?project=" in result["links"]["decisions"]
    assert h.storage.repo.commits[h.storage.repo.head]["message"].startswith(
        "Onboarding picks: systems ai-visibility; control review_in_tin"
    )
    readiness = plan_readiness(plan_at_head(h))
    assert readiness["systems"] == ["ai-visibility"] and readiness["control"] == "review_in_tin"
    assert readiness["connections"]["infra.github"]["state"] == "connected"
    assert readiness["connections"]["analytics.gsc"]["state"] == "open"

    writes = h.storage.repo.writes
    again = await call(h, "record_onboarding_picks", **args)
    assert again["replayed"] is True and again["revision"] == result["revision"]
    assert h.storage.repo.writes == writes


async def test_connected_is_verified_against_the_project(publication_db, monkeypatch):
    h = await harness(publication_db, monkeypatch)
    head = h.storage.repo.head
    text = await refused(
        h,
        "record_onboarding_picks",
        **picks_args(h, connections=[{"provider": "analytics.gsc", "decision": "connected"}]),
    )
    assert "not_connected: analytics.gsc is not connected" in text
    assert "status: none" in text
    assert h.storage.repo.head == head


async def test_unknown_picks_are_named(publication_db, monkeypatch):
    h = await harness(publication_db, monkeypatch)
    text = await refused(h, "record_onboarding_picks", **picks_args(h, systems=["paid-ads"]))
    assert "unknown_pick" in text and "technical-seo, ai-visibility, outreach" in text
    text = await refused(
        h,
        "record_onboarding_picks",
        **picks_args(h, connections=[{"provider": "crm.hubspot", "decision": "not_now"}]),
    )
    assert "unknown_pick: Unknown connection 'crm.hubspot'" in text
    text = await refused(
        h,
        "record_onboarding_picks",
        **picks_args(h, connections=[{"provider": "infra.github", "decision": "yes"}]),
    )
    assert "connections.0.decision" in text


async def test_picks_need_an_onboarding_run(publication_db, monkeypatch):
    h = await harness(publication_db, monkeypatch, executor="codex.procedure")
    assert "wrong_workflow" in await refused(h, "record_onboarding_picks", **picks_args(h))


async def test_picks_need_a_run_that_is_waiting(publication_db, monkeypatch):
    h = await harness(publication_db, monkeypatch, hold=False)
    assert "not_waiting" in await refused(h, "record_onboarding_picks", **picks_args(h))


async def test_picks_need_project_access(publication_db, monkeypatch):
    h = await harness(publication_db, monkeypatch)
    h.token.subject = OUTSIDER
    assert "project not found" in await refused(h, "record_onboarding_picks", **picks_args(h))


async def test_approval_waits_for_the_picks_on_both_surfaces(publication_db, monkeypatch):
    h = await harness(publication_db, monkeypatch)
    text = await refused(h, "approve_workflow_run", run_id=str(h.run.id))
    assert "no_systems" in text and "record_onboarding_picks" in text
    assert "technical-seo, ai-visibility, outreach" in text
    h.handle.signal.assert_not_awaited()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=h.app), base_url="https://tin.test"
    ) as client:
        response = await client.post(f"/api/workflows/runs/{h.run.id}/approve")
        assert response.status_code == 409
        assert "record_onboarding_picks" in response.json()["detail"]
    h.handle.signal.assert_not_awaited()

    await call(h, "record_onboarding_picks", **picks_args(h))
    approved = await call(h, "approve_workflow_run", run_id=str(h.run.id))
    assert approved["review_decision"] == "approval_signaled"
    h.handle.signal.assert_awaited_once_with("approve")


async def test_a_plan_ticked_without_control_is_still_refused(publication_db, monkeypatch):
    ticked_only = PLAN.replace("- [x] control: review_in_tin", "- [ ] control: review_in_tin")
    h = await harness(publication_db, monkeypatch, plan=ticked_only)
    text = await refused(h, "approve_workflow_run", run_id=str(h.run.id))
    assert "no_control" in text
    h.handle.signal.assert_not_awaited()


async def test_pick_receipt_survives_intervening_edits_and_approval(publication_db, monkeypatch):
    h = await harness(publication_db, monkeypatch)
    args = picks_args(h)
    first = await call(h, "record_onboarding_picks", **args)
    await call(h, "record_onboarding_picks", **picks_args(h, systems=["outreach"]))
    await call(h, "approve_workflow_run", run_id=str(h.run.id))
    writes = h.storage.repo.writes
    replay = await call(h, "record_onboarding_picks", **args)
    assert replay == {**first, "replayed": True}
    assert h.storage.repo.writes == writes
    assert plan_readiness(plan_at_head(h))["systems"] == ["outreach"]
    assert "request_conflict" in await refused(
        h, "record_onboarding_picks", **{**args, "systems": ["technical-seo"]}
    )
    assert "already_approved" in await refused(h, "record_onboarding_picks", **picks_args(h))


async def test_noop_picks_still_bind_request_and_caller(publication_db, monkeypatch):
    h = await harness(publication_db, monkeypatch)
    await call(h, "record_onboarding_picks", **picks_args(h))
    args = picks_args(h)
    first = await call(h, "record_onboarding_picks", **args)
    assert not first["replayed"]
    assert "request_conflict" in await refused(
        h, "record_onboarding_picks", **{**args, "systems": ["outreach"]}
    )
    h.token.client_id = "different_client"
    assert "request_conflict" in await refused(h, "record_onboarding_picks", **args)


async def test_picks_reconcile_lost_file_response_with_original_mutation(
    publication_db, monkeypatch
):
    from tin_lite.project_files import ProjectFileService

    h = await harness(publication_db, monkeypatch)
    original = ProjectFileService.commit
    lose_response = True

    async def commit(self, **kwargs):
        nonlocal lose_response
        result = await original(self, **kwargs)
        if lose_response:
            lose_response = False
            raise RuntimeError("lost response after durable Files receipt")
        return result

    monkeypatch.setattr(ProjectFileService, "commit", commit)
    args = picks_args(h)
    assert "lost response" in await refused(h, "record_onboarding_picks", **args)
    original_revision = h.storage.repo.head
    await call(h, "record_onboarding_picks", **picks_args(h, systems=["outreach"]))
    writes = h.storage.repo.writes
    result = await call(h, "record_onboarding_picks", **args)
    assert result["revision"] == original_revision
    assert h.storage.repo.writes == writes
    assert plan_readiness(plan_at_head(h))["systems"] == ["outreach"]


async def test_auto_publish_is_unavailable_in_tool_and_direct_plan_edits(
    publication_db, monkeypatch
):
    h = await harness(publication_db, monkeypatch)
    assert "control" in await refused(
        h, "record_onboarding_picks", **picks_args(h, control="auto_publish")
    )
    h.storage.repo.edit(
        {
            PLAN_PATH: PLAN.replace("- [x] control: review_in_tin", "- [ ] control: review_in_tin")
            .replace("- [ ] control: auto_publish", "- [x] control: auto_publish")
            .encode()
        },
        "edit",
    )
    assert "no_control" in await refused(h, "approve_workflow_run", run_id=str(h.run.id))
    h.handle.signal.assert_not_awaited()
