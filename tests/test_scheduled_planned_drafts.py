"""Weekly content.generate occurrences: disposable Postgres, no model or Temporal calls."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

from test_content_draft import fixture as draft_fixture
from test_content_draft import start
from test_private_workflows import ACTOR
from test_procedure_publication import publication_db as publication_db
from test_workflow_reviews import save

from tin_lite import content_draft
from tin_lite.activities import TinActivities
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.schedules import WorkflowSchedule
from tin_lite.workflow_definitions import ensure_schedule_allowed
from tin_lite.workflow_inputs import normalize_workflow_inputs

WEEKLY = WorkflowSchedule(
    cadence="weekly", weekdays=["tuesday"], local_time="10:00", timezone="UTC"
)


def test_planned_drafting_accepts_a_weekly_schedule():
    spec = next(w for w in BUILTIN_WORKFLOWS if w.key == content_draft.KEY)
    ensure_schedule_allowed(spec.definition, WEEKLY)


async def weekly(f, **inputs):
    schema = f.workflow.definition["input_schema"]
    configured = await f.db.create_project_workflow(
        project_id=f.project.id,
        workflow_id=f.workflow.id,
        definition_commit_sha="e" * 40,
        name="Weekly article",
        inputs=normalize_workflow_inputs(
            schema=schema,
            project_id=f.project.id,
            inputs={"program_id": str(f.configured.id), **inputs},
        ),
        input_schema=schema,
        schedule=WEEKLY.model_dump(mode="json"),
        request_id=uuid4(),
        created_by_clerk_user_id=ACTOR,
        pinned_definition=f.workflow.definition,
    )
    configured = await f.db.project_workflow_synced(
        project_workflow_id=configured.id,
        temporal_schedule_id=f"tin-lite-project-workflow:{configured.id}",
        next_run_at=None,
    )
    pause = AsyncMock()
    common = TinActivities(
        database=f.db,
        storage=f.storage,
        sandboxes=None,
        settings=f.settings,
        integrations=f.runtime.integrations,
        temporal=SimpleNamespace(
            get_schedule_handle=Mock(return_value=SimpleNamespace(pause=pause))
        ),
    )
    return configured, common, pause


def occurrence(configured, name="weekly-1"):
    return {
        "project_workflow_id": str(configured.id),
        "scheduled_for": datetime.now(UTC).isoformat(),
        "occurrence_id": name,
    }


async def runs(f, configured):
    return await f.db.pool.fetchval(
        "SELECT count(*) FROM workflow_runs WHERE project_workflow_id=$1", configured.id
    )


async def test_weekly_occurrence_drafts_the_next_article_in_plan_order(publication_db, monkeypatch):
    f = await draft_fixture(publication_db, monkeypatch, clean=True)
    configured, common, _ = await weekly(f)
    payload = occurrence(configured)
    dispatched = await common.dispatch_scheduled_workflow(payload)
    run = await f.db.get_run(UUID(dispatched["run_id"]))
    assert run.trigger_source == "schedule" and run.project_workflow_id == configured.id
    selected = (await f.db.get_effect(content_draft.selection_key(run.id))).result
    assert selected["mode"] == "next" and selected["item"]["id"] == f.inputs["item_id"]
    assert selected["delivery"] is None  # The program's saved settings: draft-only here.
    # A retried dispatch reuses its occurrence instead of selecting again.
    assert await common.dispatch_scheduled_workflow(payload) == dispatched
    assert await runs(f, configured) == 1


async def test_weekly_occurrence_holds_while_a_draft_waits_for_review(publication_db, monkeypatch):
    f = await draft_fixture(publication_db, monkeypatch, clean=True)
    waiting = await save(f, await start(f))
    assert waiting.status.value == "needs_input"
    configured, common, pause = await weekly(f)
    assert await common.dispatch_scheduled_workflow(occurrence(configured)) == {}
    assert await runs(f, configured) == 0
    saved = await f.db.get_project_workflow(configured.id)
    assert saved.status == "active" and saved.next_run_at is not None
    pause.assert_not_awaited()


async def test_weekly_schedule_pauses_with_a_reason_when_the_plan_is_used_up(
    publication_db, monkeypatch
):
    f = await draft_fixture(publication_db, monkeypatch, clean=True)
    discovery = await f.service.discover(project_id=f.project.id, program_id=f.configured.id)
    for index, item in enumerate(discovery["items"]):
        if item["readiness"] == "deferred":
            continue
        run = await start(f, inputs={**f.inputs, "item_id": item["id"]}, key=f"covered-{index}")
        await save(f, run, assessment=True)
    configured, common, pause = await weekly(f)
    assert await common.dispatch_scheduled_workflow(occurrence(configured)) == {}
    assert await runs(f, configured) == 0
    saved = await f.db.get_project_workflow(configured.id)
    assert saved.status == "paused"
    assert saved.last_error.startswith("Every planned article has a draft or is already covered.")
    pause.assert_awaited_once()


async def test_weekly_schedule_with_a_chosen_article_pauses_instead_of_redrafting_it(
    publication_db, monkeypatch
):
    f = await draft_fixture(publication_db, monkeypatch, clean=True)
    configured, common, _ = await weekly(f, item_id=f.inputs["item_id"])
    assert await common.dispatch_scheduled_workflow(occurrence(configured)) == {}
    assert await runs(f, configured) == 0
    saved = await f.db.get_project_workflow(configured.id)
    assert saved.status == "paused"
    assert saved.last_error.startswith("A weekly schedule drafts the next article in plan order.")
