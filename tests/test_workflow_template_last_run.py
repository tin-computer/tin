from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from tin_lite.db import Database, apply_migrations


@pytest.fixture
async def template_db():
    """Real migrations/SQL in a disposable schema, never the configured product DB."""
    dsn = os.environ.get("TIN_LITE_TEST_DATABASE_DSN")
    if not dsn:
        pytest.skip("set TIN_LITE_TEST_DATABASE_DSN for isolated Postgres contract tests")
    schema = "template_last_run_test_" + uuid4().hex
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


async def _insert_run(database, *, project_id, workflow_id, status, finished_at):
    run_id = uuid4()
    await database.pool.execute(
        """INSERT INTO workflow_runs (id, project_id, workflow_id, executor, definition_commit_sha,
            temporal_workflow_id, thread_id, generation, fencing_token, status, lease_active,
            expected_head_sha, finished_at)
           VALUES ($1, $2, $3, 'workflow.code', $4, $5, $6, 1, 1, $7, false, $8, $9)""",
        run_id,
        project_id,
        workflow_id,
        "d" * 40,
        f"code:{run_id}",
        str(run_id),
        status,
        "a" * 40,
        finished_at,
    )
    return run_id


async def test_template_state_names_the_latest_finished_run_in_this_project(template_db):
    database = template_db
    project = await database.create_project(name="Last run", state_repo_id="projects/last-run")
    other = await database.create_project(name="Other", state_repo_id="projects/other")
    workflow_id = uuid4()
    never_run_id = uuid4()
    for key, identifier in (("organic.keyword_plan", workflow_id), ("organic.audit", never_run_id)):
        await database.pool.execute(
            """INSERT INTO workflows (id, key, title, executor, definition_repo_id, definition_path,
                    current_commit_sha, version_label, definition)
               VALUES ($1, $2, $2, 'workflow.code', 'registry/workflows', 'w.json', $3,
                       '1', '{}')""",
            identifier,
            key,
            "d" * 40,
        )
    now = datetime.now(UTC)
    await _insert_run(
        database,
        project_id=project.id,
        workflow_id=workflow_id,
        status="failed",
        finished_at=now - timedelta(days=2),
    )
    latest = await _insert_run(
        database,
        project_id=project.id,
        workflow_id=workflow_id,
        status="succeeded",
        finished_at=now - timedelta(days=1),
    )
    # A run still going and another project's newer run are not this project's last run.
    await _insert_run(
        database,
        project_id=project.id,
        workflow_id=workflow_id,
        status="running",
        finished_at=None,
    )
    await _insert_run(
        database,
        project_id=other.id,
        workflow_id=workflow_id,
        status="succeeded",
        finished_at=now,
    )

    state = await database.get_workflow_template_state(
        project_id=project.id, clerk_user_id="user_test"
    )

    assert state[workflow_id]["last_run_id"] == latest
    assert state[workflow_id]["last_run_at"] == now - timedelta(days=1)
    assert state[never_run_id]["last_run_id"] is None
    assert state[never_run_id]["last_run_at"] is None
