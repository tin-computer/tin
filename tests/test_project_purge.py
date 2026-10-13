"""Deleting a project removes what it owns, including the people it collected."""

import inspect
import re
from uuid import uuid4

from test_connection_collection import ACTOR, TOKEN, batch, seed
from test_procedure_publication import publication_db as publication_db

from tin_lite.db import Database

# Tables that deliberately outlive a deleted project, behind its tombstone.
KEPT = {
    # The tombstone itself, and the runs and saved workflows the billing ledger references.
    "projects",
    "workflow_runs",
    "workflows",
    "project_workflows",
    "billing_ledger",
    "billing_operations",
    "billing_project_policies",
    "billing_quotes",
    "billing_run_budgets",
    "run_payment_cards",
    # Provider deliveries stay with the provider; the purge clears their project link.
    "integration_webhook_deliveries",
    # No personal data. Whether Tin keeps its record of the Google Ads campaigns it created
    # after the project is gone is a paid-ads decision, made separately.
    "paid_ads_campaigns",
    "paid_ads_proposals",
}


# What the seeded project owns that the purge must remove: its run's rows, then its own.
OWNED = """SELECT
    (SELECT count(*) FROM connection_collection_pages WHERE run_id = $1) AS pages,
    (SELECT count(*) FROM connection_collection_account_leases WHERE run_id = $1) AS leases,
    (SELECT count(*) FROM project_task_entries WHERE run_id = $1) AS task_entries,
    (SELECT count(*) FROM connection_collection_jobs WHERE project_id = $2) AS jobs,
    (SELECT count(*) FROM connection_extension_devices WHERE project_id = $2) AS devices,
    (SELECT count(*) FROM project_authors WHERE project_id = $2) AS authors"""


def cleaned_by_deletion() -> set[str]:
    source = inspect.getsource(Database.purge_project_data) + inspect.getsource(
        Database.revoke_project_access
    )
    return set(re.findall(r"DELETE FROM (\w+)", source))


async def test_every_project_table_is_purged_or_kept_on_purpose(publication_db):
    """A new table that holds a project's data must join the purge or KEPT, never neither."""
    rows = await publication_db.pool.fetch(
        """
        SELECT c.relname AS name,
               EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid
                       AND a.attname = 'project_id' AND NOT a.attisdropped) AS has_project_id
        FROM pg_class c
        WHERE c.relkind = 'r' AND c.relnamespace = current_schema()::regnamespace
        """
    )
    edges = await publication_db.pool.fetch(
        """
        SELECT child.relname AS child, parent.relname AS parent, con.confdeltype::text AS action
        FROM pg_constraint con
        JOIN pg_class child ON child.oid = con.conrelid
        JOIN pg_class parent ON parent.oid = con.confrelid
        WHERE con.contype = 'f' AND child.relnamespace = current_schema()::regnamespace
        """
    )
    tables = {row["name"] for row in rows}
    scoped = {row["name"] for row in rows if row["has_project_id"]}
    owners = {"projects", "workflow_runs", "integration_connections"}
    changed = True
    while changed:
        before = len(scoped)
        # Rows hanging off a kept table (a user's saved templates, a welcome grant) are not the
        # project's, so scope spreads only through what the project owns outright.
        scoped |= {e["child"] for e in edges if e["parent"] in owners | (scoped - KEPT)}
        changed = len(scoped) != before
    cleaned = cleaned_by_deletion()
    changed = True
    while changed:
        before = len(cleaned)
        cleaned |= {e["child"] for e in edges if e["parent"] in cleaned and e["action"] == "c"}
        changed = len(cleaned) != before
    assert not KEPT - tables, f"KEPT names missing tables: {', '.join(sorted(KEPT - tables))}"
    both = KEPT & cleaned_by_deletion()
    assert not both, f"purged tables listed in KEPT: {', '.join(sorted(both))}"
    left = scoped - cleaned - KEPT - {"projects"}
    assert not left, f"project data the purge leaves behind: {', '.join(sorted(left))}"


async def test_purge_removes_collected_people_authors_and_task_transcripts(publication_db):
    f = await seed(publication_db)
    db = f.db
    # A collection interrupted mid-lease: the job still holds its device.
    claim = await f.store.claim(f.run.id, f.project.id, TOKEN, ACTOR["key"])
    await f.store.page(f.run.id, f.project.id, TOKEN, batch(claim))
    other = await db.create_project(name="Kept", state_repo_id=f"projects/{uuid4()}")
    for project in (f.project, other):
        await db.pool.execute(
            """INSERT INTO project_authors (project_id, id, display_name, guide_path,
                   confirmed_by_clerk_user_id)
               VALUES ($1, $2, 'Founder', $3, 'user_owner')""",
            project.id,
            uuid4(),
            f".agents/skills/authors/{uuid4()}/SKILL.md",
        )
    await db.pool.execute(
        """INSERT INTO project_task_entries (id, run_id, kind, source, content)
           VALUES ($1, $2, 'message', 'codex', 'A task transcript line')""",
        uuid4(),
        f.run.id,
    )

    before = await db.pool.fetchrow(OWNED, f.run.id, f.project.id)
    assert all(before.values()), dict(before)

    async with db.pool.acquire() as conn:
        await db.purge_project_data(conn, project_id=f.project.id)
        await db.purge_project_data(conn, project_id=f.project.id)

    left = await db.pool.fetchrow(OWNED, f.run.id, f.project.id)
    assert dict(left) == dict.fromkeys(left.keys(), 0)
    assert (
        await db.pool.fetchval(
            "SELECT count(*) FROM project_authors WHERE project_id = $1", other.id
        )
        == 1
    )
    # Runs stay behind the tombstone for the billing ledger.
    assert await db.pool.fetchval("SELECT count(*) FROM workflow_runs WHERE id = $1", f.run.id) == 1
