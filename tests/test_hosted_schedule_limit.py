"""Hosted default policies carry standing schedule authority; disposable Postgres only."""

from pathlib import Path
from uuid import uuid4

import pytest
from test_billing import ACTOR, finish, fund
from test_billing import billed as billed
from test_incremental_billing import direct
from test_procedure_publication import publication_db as publication_db
from test_service_billing import SITE, install

from tin_lite.billing_contracts import BillingError, ProjectSpendingPolicy
from tin_lite.workflow_inputs import normalize_workflow_inputs

DEFAULT = 10_000_000_000
MIGRATION = Path(__file__).parents[1] / "migrations" / "047_hosted_schedule_limit.sql"


def test_backfill_only_targets_untouched_hosted_defaults():
    sql = MIGRATION.read_text()
    assert "UPDATE billing_project_policies" in sql
    for guard in (
        "schedule_max_nanos IS NULL",
        "revision = 1",
        "per_run_nanos = 10000000000",
        "monthly_nanos = 10000000000",
        "concurrency = 1",
    ):
        assert guard in sql


async def saved_schedule(f, key="organic.audit"):
    workflow = await install(f, key)
    saved = await f.db.create_project_workflow(
        project_id=f.project.id,
        workflow_id=workflow.id,
        name="Scheduled audit",
        inputs=normalize_workflow_inputs(
            schema=workflow.definition["input_schema"], project_id=f.project.id, inputs=SITE
        ),
        definition_commit_sha=workflow.current_commit_sha,
        input_schema=workflow.definition["input_schema"],
        schedule=None,
        request_id=uuid4(),
        created_by_clerk_user_id=ACTOR,
    )
    await f.db.project_workflow_synced(
        project_workflow_id=saved.id, temporal_schedule_id=None, next_run_at=None
    )
    return saved


async def test_hosted_default_policy_funds_scheduled_runs_until_cleared(billed):
    f = billed
    await fund(f)
    await f.db.pool.execute(
        "DELETE FROM billing_project_policies WHERE project_id=$1", f.project.id
    )
    f.settings.billing_hosted_defaults_enabled = True
    await f.billing.ensure_hosted_projects(ACTOR)
    policy = await f.db.pool.fetchrow(
        "SELECT * FROM billing_project_policies WHERE project_id=$1", f.project.id
    )
    assert policy["revision"] == 1
    # New hosted projects start above migration 047's $10 defaults; saved policies keep theirs.
    assert policy["per_run_nanos"] == 25_000_000_000
    assert policy["monthly_nanos"] == 100_000_000_000
    assert policy["schedule_max_nanos"] == 50_000_000_000

    saved = await saved_schedule(f)
    run = await direct(f, "organic.audit", SITE, configured=saved)
    assert run.trigger_source == "schedule"
    await finish(f, run)

    # Clearing the allowance in Billing still turns paid schedules off.
    await f.billing.update_policy(
        f.project.id,
        ACTOR,
        ProjectSpendingPolicy(
            per_run_nanos=policy["per_run_nanos"],
            monthly_nanos=policy["monthly_nanos"],
            expected_revision=1,
            schedule_max_nanos=None,
        ),
    )
    with pytest.raises(BillingError, match="standing spending"):
        await direct(f, "organic.audit", SITE, configured=saved)


@pytest.mark.parametrize(
    ("revision", "per_run", "monthly", "concurrency", "schedule", "expected"),
    [
        (1, DEFAULT, DEFAULT, 1, None, DEFAULT),
        # Saved in Billing (revision raised), including a cleared allowance.
        (2, DEFAULT, DEFAULT, 1, None, None),
        (1, 20_000_000_000, DEFAULT, 1, None, None),
        (1, DEFAULT, 50_000_000_000, 1, None, None),
        (1, DEFAULT, DEFAULT, 2, None, None),
        (1, DEFAULT, DEFAULT, 1, 3_000_000_000, 3_000_000_000),
    ],
)
async def test_backfill_grants_schedule_authority_to_untouched_defaults_only(
    billed, revision, per_run, monthly, concurrency, schedule, expected
):
    f = billed
    await f.db.pool.execute(
        """UPDATE billing_project_policies SET revision=$2, per_run_nanos=$3,
           monthly_nanos=$4, concurrency=$5, schedule_max_nanos=$6 WHERE project_id=$1""",
        f.project.id,
        revision,
        per_run,
        monthly,
        concurrency,
        schedule,
    )
    sql = MIGRATION.read_text()
    for _ in range(2):
        await f.db.pool.execute(sql)
        row = await f.db.pool.fetchrow(
            "SELECT * FROM billing_project_policies WHERE project_id=$1", f.project.id
        )
        assert row["schedule_max_nanos"] == expected
        assert (row["revision"], row["per_run_nanos"], row["monthly_nanos"]) == (
            revision,
            per_run,
            monthly,
        )
        assert row["concurrency"] == concurrency
