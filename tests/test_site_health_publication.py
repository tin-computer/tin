"""Site health publication: a generic no-change result skips the PR but keeps a receipt."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from test_procedure_publication import activity_fixture
from test_procedure_publication import publication_db as publication_db

from tin_lite.activities import blocked_by_setup
from tin_lite.procedures import (
    GITHUB_PULL_REQUEST_RESULT,
    GITHUB_REPOSITORY_WORKSPACE,
    PinnedCodexProcedure,
    procedure_checkpoint_path,
)


def site_manifest(*, no_change: bool, title: str | None = None) -> dict:
    return {
        "repository": "owner/site",
        "default_branch": "main",
        "head_sha": "b" * 40,
        "title": title
        or ("No change: metadata is complete" if no_change else "Add a page description"),
        "body": "Inspected the homepage; nothing bounded to fix." if no_change else "One fix.",
        "files": [] if no_change else [{"path": "app/layout.tsx", "content": "export {}\n"}],
        "outcome": "no_change" if no_change else "patch",
        "verification": ["git diff --check"],
    }


async def site_health_fixture(db, monkeypatch, manifest):
    activities, storage, run, _checkpoint = await activity_fixture(db)
    await db.pool.execute(
        "UPDATE workflows SET key='site.health_improve' WHERE id=$1", run.workflow_id
    )
    await db.pool.execute("UPDATE workflow_runs SET lease_active=true WHERE id=$1", run.id)
    await db.pool.execute(
        "UPDATE effect_receipts SET result=$2::jsonb WHERE execution_key=$1",
        f"{run.id}:procedure_artifact_persist",
        json.dumps({"ephemeral_commit_sha": "e" * 40, "summary": "untrusted model summary"}),
    )
    contract = PinnedCodexProcedure(
        workflow_key="site.health_improve",
        prompt="Improve the site.",
        entry_skill="site-health-improvement",
        skill_files={},
        result_kind=GITHUB_PULL_REQUEST_RESULT,
        workspace_kind=GITHUB_REPOSITORY_WORKSPACE,
        provider_key="infra.github",
        output_max_files=3,
        receipt_path_template="reports/site-health/{run_id}.md",
        verification_commands=("git diff --check",),
        allow_no_change=True,
    )
    monkeypatch.setattr(
        activities,
        "_pinned_codex_procedure",
        AsyncMock(
            return_value=(
                SimpleNamespace(key="site.health_improve", title="Improve site health"),
                contract,
            )
        ),
    )
    read_checkpoint = AsyncMock(return_value=json.dumps(manifest).encode())
    monkeypatch.setattr(storage, "read_ephemeral_artifact", read_checkpoint)
    publisher = AsyncMock(return_value=("f" * 40, True))
    monkeypatch.setattr(storage, "publish_state_document", publisher)
    create_pr = AsyncMock(
        return_value=SimpleNamespace(
            url="https://github.com/owner/site/pull/1",
            number=1,
            repository="owner/site",
            branch="tin/site-health",
        )
    )
    activities._integrations = SimpleNamespace(github_create_pull_request=create_pr)
    return activities, run, read_checkpoint, publisher, create_pr


@pytest.mark.parametrize("no_change", [False, True])
async def test_site_health_publication_skips_pr_for_no_change(
    publication_db, monkeypatch, no_change
):
    db = publication_db
    activities, run, read_checkpoint, publisher, create_pr = await site_health_fixture(
        db, monkeypatch, site_manifest(no_change=no_change)
    )
    pause = AsyncMock()
    monkeypatch.setattr(activities, "_pause_blocked_schedule", pause)

    await activities.commit_codex_procedure_artifact(str(run.id))
    await activities.commit_codex_procedure_artifact(str(run.id))

    assert read_checkpoint.await_args.kwargs["path"] == procedure_checkpoint_path(run.id)
    assert publisher.await_count == 1
    assert publisher.await_args.kwargs["path"] == f"reports/site-health/{run.id}.md"
    receipt = publisher.await_args.kwargs["content"].decode()
    assert create_pr.await_count == (0 if no_change else 1)
    saved = await db.get_effect(f"{run.id}:procedure_canonical_commit")
    assert saved.status == "completed"
    if no_change:
        assert saved.result["summary"] != "untrusted model summary"
        assert saved.result["outcome"] == "no_change"
        assert saved.result["repository"] == "owner/site"
        assert saved.result["summary"].startswith("No change proposed")
        assert "external_url" not in saved.result
        assert "No change proposed; no pull request was opened." in receipt
        assert "**No change: metadata is complete**" in receipt
        events = await db.pool.fetch(
            "SELECT event_type FROM activity_events WHERE run_id=$1 ORDER BY id", run.id
        )
        assert [e["event_type"] for e in events].count("codex_procedure_no_change") == 1
        assert "needs_you" not in saved.result
    else:
        assert "outcome" not in saved.result
        assert saved.result["external_url"] == "https://github.com/owner/site/pull/1"
        assert "- `app/layout.tsx`" in receipt
        assert create_pr.await_args.kwargs["expected_base_sha"] == "b" * 40
    assert not (await db.get_run(run.id)).lease_active
    # An ordinary quiet week never pauses the schedule.
    pause.assert_not_awaited()


async def test_a_run_stopped_by_setup_says_what_to_fix_and_pauses_its_schedule(
    publication_db, monkeypatch
):
    # A ClawMessenger run stopped for weeks on a stale repository name and read as "no change".
    fix = "the selected repository does not serve www.example.com; select the site's repository"
    activities, run, _read, publisher, create_pr = await site_health_fixture(
        publication_db,
        monkeypatch,
        site_manifest(no_change=True, title=f"No change: needs you: {fix}"),
    )
    pause = AsyncMock()
    monkeypatch.setattr(activities, "_pause_blocked_schedule", pause)

    await activities.commit_codex_procedure_artifact(str(run.id))
    await activities.commit_codex_procedure_artifact(str(run.id))  # A retry pauses again, safely.

    create_pr.assert_not_awaited()
    assert publisher.await_count == 1
    saved = await publication_db.get_effect(f"{run.id}:procedure_canonical_commit")
    assert saved.result["outcome"] == "no_change"
    assert saved.result["needs_you"] == fix
    assert saved.result["summary"] == f"Needs you: {fix}"
    assert [call.args for call in pause.await_args_list] == [(run.id, fix), (run.id, fix)]


async def test_only_a_scheduled_saved_workflow_is_paused(publication_db, monkeypatch):
    activities, run, *_ = await site_health_fixture(
        publication_db, monkeypatch, site_manifest(no_change=True)
    )
    import tin_lite.code_schedules as code_schedules

    paused = AsyncMock()
    monkeypatch.setattr(code_schedules, "pause_for_issue", paused)
    configured = SimpleNamespace(status="active", schedule={"cadence": "weekly"}, last_error=None)
    monkeypatch.setattr(activities._db, "get_project_workflow", AsyncMock(return_value=configured))

    # A one-off run has no saved workflow to pause.
    monkeypatch.setattr(
        activities,
        "_require_run",
        AsyncMock(return_value=SimpleNamespace(project_workflow_id=None)),
    )
    await activities._pause_blocked_schedule(run.id, "fix it")
    paused.assert_not_awaited()

    saved = SimpleNamespace(project_workflow_id=uuid4())
    monkeypatch.setattr(activities, "_require_run", AsyncMock(return_value=saved))
    await activities._pause_blocked_schedule(run.id, "fix it")
    paused.assert_awaited_once_with(activities, configured, "fix it")

    # An unscheduled saved workflow runs only when started; there is nothing to pause.
    paused.reset_mock()
    configured.schedule = None
    await activities._pause_blocked_schedule(run.id, "fix it")
    paused.assert_not_awaited()


@pytest.mark.parametrize(
    ("title", "fix"),
    [
        ("No change: needs you: reconnect GitHub", "reconnect GitHub"),
        ("no change:  needs YOU:   select the repository ", "select the repository"),
        ("No change: metadata is complete", None),
        ("No change: needs you:", None),
        (None, None),
    ],
)
def test_needs_you_titles(title, fix):
    assert blocked_by_setup(title) == fix
