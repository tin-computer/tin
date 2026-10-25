"""Final report progress uses real SQL, synthetic artifacts and no provider calls."""

import pytest
from test_procedure_publication import activity_fixture
from test_procedure_publication import publication_db as publication_db

from tin_lite.domain import SideEffectConflictError


async def report_fixture(db, workflow_key, *, current=2, total=3):
    _, _, run, _ = await activity_fixture(db)
    await db.pool.execute(
        "UPDATE workflow_runs SET executor=$2, lease_active=false WHERE id=$1",
        run.id,
        workflow_key,
    )
    await db.project_run_progress(
        run_id=run.id,
        mode="steps" if total is not None else "indeterminate",
        step="publish" if workflow_key == "ads.assessment" else "compose",
        current=current,
        total=total,
        percent=67,
        summary="Still preparing the report.",
    )
    return run


async def complete_report(db, run, workflow_key, *, summary="The report is ready.", failed=False):
    key = f"{run.id}:report_projection"
    async with db.effect_lock(key, workflow_key) as (conn, receipt):
        if receipt and receipt.status == "completed":
            return
        await db.start_effect(conn, execution_key=key, operation=workflow_key)
        await db._complete_readonly_report_projection(
            conn,
            execution_key=key,
            run_id=run.id,
            canonical_commit_sha="a" * 40,
            artifact_path="reports/RESULT.md",
            artifact_ref="code.storage:fixture-result",
            summary=summary,
            workflow_key=workflow_key,
            final_status="failed" if failed else "succeeded",
        )


@pytest.mark.parametrize(
    ("workflow_key", "current", "total", "summary", "expected_summary"),
    [
        ("social.x_draft", 2, 3, "Your X draft is ready.", "Your X draft is ready."),
        ("ads.assessment", 4, 5, "The assessment is ready.", "The assessment is ready."),
        ("organic.audit", 2, 3, "Inspected the selected pages.", "Inspected the selected pages."),
        ("social.x_draft", None, None, None, "Your X draft is ready."),
    ],
)
async def test_success_finishes_known_counts_and_keeps_unknown_counts(
    publication_db, workflow_key, current, total, summary, expected_summary
):
    db = publication_db
    run = await report_fixture(db, workflow_key, current=current, total=total)
    before_events = await db.pool.fetchval(
        "SELECT count(*) FROM activity_events WHERE run_id=$1", run.id
    )
    for _ in range(2):
        await complete_report(db, run, workflow_key, summary=summary)
    done = await db.get_run(run.id)
    assert done.status.value == "succeeded"
    assert done.progress_step == "complete"
    assert done.progress_current == done.progress_total == total
    assert done.progress_percent == 100
    assert done.progress_summary == expected_summary
    assert done.progress_mode == ("steps" if total is not None else "indeterminate")
    assert done.canonical_commit_sha == "a" * 40
    assert done.artifact_path == "reports/RESULT.md"
    assert done.artifact_ref == "code.storage:fixture-result"
    assert (
        await db.pool.fetchval("SELECT count(*) FROM activity_events WHERE run_id=$1", run.id)
        == before_events + 1
    )
    assert (await db.get_effect(f"{run.id}:report_projection")).status == "completed"
    assert not await db.project_run_progress(
        run_id=run.id,
        mode="steps",
        step="compose",
        current=2,
        total=3,
        percent=67,
        summary="Late progress must not replace completion.",
    )
    assert (await db.get_run(run.id)).progress_step == "complete"


async def test_failed_report_keeps_its_last_step_counts_and_progress_summary(publication_db):
    db = publication_db
    run = await report_fixture(db, "organic.traffic_system")
    await complete_report(db, run, "organic.traffic_system", summary="A child failed.", failed=True)
    failed = await db.get_run(run.id)
    assert failed.status.value == "failed"
    assert failed.progress_step == "compose"
    assert failed.progress_current == 2 and failed.progress_total == 3
    assert failed.progress_summary == "Still preparing the report."
    assert failed.error_message == "A child failed."
    assert (
        await db.pool.fetchval(
            "SELECT count(*) FROM activity_events WHERE run_id=$1 "
            "AND event_type='organic_system_incomplete'",
            run.id,
        )
        == 1
    )


@pytest.mark.parametrize("status", ["failed", "stopped", "superseded", "needs_input"])
async def test_completion_does_not_change_terminal_or_unapproved_progress(publication_db, status):
    db = publication_db
    run = await report_fixture(db, "social.x_draft")
    before_events = await db.pool.fetchval(
        "SELECT count(*) FROM activity_events WHERE run_id=$1", run.id
    )
    await db.pool.execute(
        "UPDATE workflow_runs SET status=$2, review_required=$3 WHERE id=$1",
        run.id,
        status,
        status == "needs_input",
    )
    with pytest.raises(SideEffectConflictError, match="cannot complete"):
        await complete_report(db, run, "social.x_draft")
    unchanged = await db.get_run(run.id)
    assert unchanged.status.value == status
    assert unchanged.progress_step == "compose"
    assert unchanged.progress_current == 2 and unchanged.progress_total == 3
    assert unchanged.progress_percent == 67
    assert unchanged.progress_summary == "Still preparing the report."
    assert unchanged.artifact_path is None
    assert unchanged.canonical_commit_sha is None
    assert (
        await db.pool.fetchval("SELECT count(*) FROM activity_events WHERE run_id=$1", run.id)
        == before_events
    )
