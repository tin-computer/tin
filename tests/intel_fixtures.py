"""Shared synthetic rows for the daily intelligence tests (no customer data)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

from tin_lite import intel_store

DAY = date(2026, 11, 24)
NOW = datetime(2026, 11, 24, 6, 0, tzinfo=UTC)
USER = "user_inteltest"


async def project(db, name="Intel host") -> UUID:
    created = await db.create_project(name=name, state_repo_id="projects/" + uuid4().hex)
    await db.grant_project_membership(project_id=created.id, clerk_user_id=USER)
    return created.id


async def run(db, project_id: UUID, executor: str, *, status="running", inputs=None) -> UUID:
    workflow = await db.pool.fetchval("SELECT id FROM workflows WHERE key = $1", executor)
    if workflow is None:
        workflow = await db.pool.fetchval(
            """INSERT INTO workflows (id, key, title, executor, definition_repo_id,
                   definition_path, current_commit_sha, version_label, definition)
               VALUES ($1, $2, $2, $3, 'registry/workflows', $2 || '.json', $4, 'v1', '{}')
               RETURNING id""",
            uuid4(),
            executor,
            "codex.procedure" if executor.endswith("_judge") else executor,
            "d" * 40,
        )
    run_id = uuid4()
    await db.pool.execute(
        """INSERT INTO workflow_runs (id, project_id, workflow_id, executor, definition_commit_sha,
               temporal_workflow_id, thread_id, generation, fencing_token, status,
               started_by_clerk_user_id, input)
           VALUES ($1, $2, $3, $4, $5, $6, $6, 1, 1, $7, $8, $9::jsonb)""",
        run_id,
        project_id,
        workflow,
        "codex.procedure" if executor.endswith("_judge") else executor,
        "d" * 40,
        f"intel-{run_id}",
        status,
        USER,
        json.dumps({"project_id": str(project_id), **(inputs or {})}),
    )
    return run_id


async def fetch(db, run_id: UUID, *, scope="world", project_id=None, day=DAY) -> None:
    await intel_store.begin_fetch(
        db.pool,
        run_id=run_id,
        scope=scope,
        project_id=project_id,
        day=day,
        window_start=NOW - timedelta(hours=24),
        window_end=NOW,
        config_sha256="a" * 64,
    )


def item(url: str, *, source="BBC", rank=1, title=None, text="Body text.", **extra):
    return {
        "source_type": extra.pop("source_type", "world"),
        "source": source,
        "source_id": extra.pop("source_id", source.lower().replace(" ", "_")),
        "source_url": "https://feeds.example/rss",
        "url": url,
        "discussion_url": extra.pop("discussion_url", None),
        "publisher_site": None,
        "title": title or f"Story at {url}",
        "author": None,
        "published_at": "2026-11-24T03:00:00Z",
        "text": text,
        "engagement": extra.pop("engagement", {}),
        "rank": rank,
        "fetched_at": "2026-11-24T06:00:00Z",
    }
