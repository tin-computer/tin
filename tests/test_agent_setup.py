"""The locked dashboard follows the coding agent: connected, setup started, plan ready."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest
from fastapi import FastAPI

from tin_lite.api import router
from tin_lite.auth import AuthContext, require_user
from tin_lite.db import Database, apply_migrations

USER = "user_founder"
OTHER = "user_teammate"


class FakeDatabase:
    def __init__(self, members: set[str]) -> None:
        self.members = members
        self.asked: list[str] = []

    async def has_project_access(self, *, project_id: UUID, clerk_user_id: str) -> bool:
        return clerk_user_id in self.members

    async def agent_setup_status(self, clerk_user_id: str) -> dict:
        self.asked.append(clerk_user_id)
        return {"agent": {"connected_at": None, "client": None}, "setup": {"plan_ready": False}}


def setup_app(database: FakeDatabase, clerk_user_id: str) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.runtime = SimpleNamespace(database=database)
    app.dependency_overrides[require_user] = lambda: AuthContext(
        clerk_user_id=clerk_user_id,
        token_type="session_token",  # noqa: S106
    )
    return app


@pytest.mark.asyncio
async def test_agent_setup_answers_members_only() -> None:
    database = FakeDatabase(members={USER})
    project_id = uuid4()
    for clerk_user_id, expected in ((USER, 200), (OTHER, 404)):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=setup_app(database, clerk_user_id)),
            base_url="http://test",
        ) as client:
            response = await client.get(f"/api/projects/{project_id}/agent-setup")
        assert response.status_code == expected
    # A stranger's request never reaches the per-person query.
    assert database.asked == [USER]


@pytest.fixture
async def setup_db():
    """Real migrations/SQL in a disposable schema, never the configured product DB."""
    dsn = os.environ.get("TIN_LITE_TEST_DATABASE_DSN")
    if not dsn:
        pytest.skip("set TIN_LITE_TEST_DATABASE_DSN for isolated Postgres contract tests")
    schema = "agent_setup_test_" + uuid4().hex
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    database = Database(dsn)
    try:
        database._pool = await asyncpg.create_pool(
            dsn, min_size=1, max_size=4, server_settings={"search_path": schema}
        )
        scoped_dsn = dsn + ("&" if "?" in dsn else "?") + f"search_path={schema}"
        await apply_migrations(scoped_dsn, Path(__file__).parents[1] / "migrations")
        yield database
    finally:
        await database.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def _workflow(database: Database, key: str) -> UUID:
    workflow_id = uuid4()
    await database.pool.execute(
        """INSERT INTO workflows (id, key, title, executor, definition_repo_id, definition_path,
                current_commit_sha, version_label, definition)
           VALUES ($1, $2, $2, $2, 'registry/workflows', 'w.json', $3, '1', '{}')""",
        workflow_id,
        key,
        "d" * 40,
    )
    return workflow_id


async def _run(
    database: Database,
    *,
    project_id: UUID,
    workflow_id: UUID,
    executor: str,
    status: str,
    started_by: str = USER,
    trigger_source: str = "mcp",
    trigger_client: str | None = "claude_code",
    created_at: datetime,
) -> UUID:
    run_id = uuid4()
    await database.pool.execute(
        """INSERT INTO workflow_runs (id, project_id, workflow_id, executor, definition_commit_sha,
                temporal_workflow_id, thread_id, generation, fencing_token, status, lease_active,
                expected_head_sha, started_by_clerk_user_id, trigger_source, trigger_client,
                created_at)
           VALUES ($1, $2, $3, $4, $5, $6, $7, 1, 1, $8, false, $9, $10, $11, $12, $13)""",
        run_id,
        project_id,
        workflow_id,
        executor,
        "d" * 40,
        f"run:{run_id}",
        str(run_id),
        status,
        "a" * 40,
        started_by,
        trigger_source,
        trigger_client,
        created_at,
    )
    return run_id


async def test_nothing_yet_reads_as_waiting(setup_db) -> None:
    database = setup_db
    personal = await database.create_project(name="Founder's project", state_repo_id="p/personal")
    await database.grant_project_membership(project_id=personal.id, clerk_user_id=USER)
    status = await database.agent_setup_status(USER)
    assert status["agent"] == {"connected_at": None, "client": None}
    assert status["setup"]["started_at"] is None
    assert status["setup"]["plan_ready"] is False


async def test_a_project_call_connects_the_agent_and_onboarding_elsewhere_is_followed(
    setup_db,
) -> None:
    database = setup_db
    personal = await database.create_project(name="Founder's project", state_repo_id="p/personal")
    business = await database.create_project(name="Acme", state_repo_id="p/acme")
    for project in (personal, business):
        await database.grant_project_membership(project_id=project.id, clerk_user_id=USER)

    # The agent made a project and called get_started on it: connected, nothing started.
    await database.record_mcp_usage(
        project_id=business.id,
        clerk_user_id=USER,
        oauth_client_id="client-1",
        tool_name="get_started",
    )
    status = await database.agent_setup_status(USER)
    assert status["agent"]["connected_at"] is not None
    assert status["agent"]["client"] is None
    assert status["setup"]["started_at"] is None

    # It started onboarding in the business project; the setup names that project.
    onboarding = await _workflow(database, "growth.onboarding")
    now = datetime.now(UTC)
    run_id = await _run(
        database,
        project_id=business.id,
        workflow_id=onboarding,
        executor="growth.onboarding",
        status="running",
        created_at=now,
    )
    status = await database.agent_setup_status(USER)
    assert status["agent"]["client"] == "claude_code"
    assert status["setup"]["run_id"] == run_id
    assert status["setup"]["project_id"] == business.id
    assert status["setup"]["project_name"] == "Acme"
    assert status["setup"]["status"] == "running"
    assert status["setup"]["plan_ready"] is False

    # Waiting for picks is a ready plan.
    await database.pool.execute("UPDATE workflow_runs SET status='needs_input' WHERE id=$1", run_id)
    assert (await database.agent_setup_status(USER))["setup"]["plan_ready"] is True


async def test_setup_ignores_superseded_runs_other_people_and_projects_they_left(setup_db) -> None:
    database = setup_db
    business = await database.create_project(name="Acme", state_repo_id="p/acme")
    shared = await database.create_project(name="Team", state_repo_id="p/team")
    stranger = await database.create_project(name="Not mine", state_repo_id="p/not-mine")
    for project in (business, shared):
        await database.grant_project_membership(project_id=project.id, clerk_user_id=USER)
    await database.grant_project_membership(project_id=shared.id, clerk_user_id=OTHER)
    await database.grant_project_membership(project_id=stranger.id, clerk_user_id=OTHER)
    onboarding = await _workflow(database, "growth.onboarding")
    now = datetime.now(UTC)

    kept = await _run(
        database,
        project_id=business.id,
        workflow_id=onboarding,
        executor="growth.onboarding",
        status="succeeded",
        trigger_client="codex",
        created_at=now - timedelta(minutes=10),
    )
    # A newer restart that was superseded, a teammate's onboarding in a shared project and
    # an onboarding in a project this person is not in do not move the setup.
    await _run(
        database,
        project_id=business.id,
        workflow_id=onboarding,
        executor="growth.onboarding",
        status="superseded",
        trigger_client="codex",
        created_at=now - timedelta(minutes=5),
    )
    await _run(
        database,
        project_id=shared.id,
        workflow_id=onboarding,
        executor="growth.onboarding",
        status="running",
        started_by=OTHER,
        created_at=now,
    )
    await _run(
        database,
        project_id=stranger.id,
        workflow_id=onboarding,
        executor="growth.onboarding",
        status="running",
        started_by=USER,
        created_at=now,
    )
    status = await database.agent_setup_status(USER)
    assert status["setup"]["run_id"] == kept
    assert status["setup"]["plan_ready"] is True
    assert status["agent"]["client"] == "codex"

    # A deleted project no longer counts.
    await database.pool.execute("UPDATE projects SET deleted_at=now() WHERE id=$1", business.id)
    status = await database.agent_setup_status(USER)
    assert status["setup"]["run_id"] is None
    assert status["agent"]["connected_at"] is None


async def test_a_browser_run_does_not_count_as_the_agent_connecting(setup_db) -> None:
    database = setup_db
    personal = await database.create_project(name="Founder's project", state_repo_id="p/personal")
    await database.grant_project_membership(project_id=personal.id, clerk_user_id=USER)
    audit = await _workflow(database, "organic.audit")
    await _run(
        database,
        project_id=personal.id,
        workflow_id=audit,
        executor="organic.audit",
        status="running",
        trigger_source="manual",
        trigger_client=None,
        created_at=datetime.now(UTC),
    )
    status = await database.agent_setup_status(USER)
    assert status["agent"]["connected_at"] is None
    assert status["setup"]["started_at"] is None
