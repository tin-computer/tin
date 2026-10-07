"""The System calendar over HTTP: a week of runs and what is still to come, per project."""

from datetime import UTC, datetime
from uuid import uuid4

import httpx
from test_private_workflows import ACTOR, activate, app, fixture
from test_procedure_publication import publication_db as publication_db

# A future week, so every Thursday slot is still to come: Monday 7 January 2030.
WEEK = "2030-01-07"
LA = "America/Los_Angeles"


async def digest_workflow(f):
    await activate(f)
    return next(
        item
        for item in await f.db.list_workflows(project_id=f.project.id)
        if item.key == "custom.research_digest"
    )


async def run_at(f, workflow, when: datetime, *, status: str = "succeeded"):
    run, _ = await f.db.create_run(
        project_id=f.project.id,
        workflow_id=workflow.id,
        started_by_clerk_user_id=ACTOR,
        input_payload={"brief": "Summarize the public docs"},
    )
    await f.db.pool.execute(
        "UPDATE workflow_runs SET created_at = $2, started_at = $2, status = $3 WHERE id = $1",
        run.id,
        when,
        status,
    )
    return run


def client(f, **kwargs):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f, **kwargs)), base_url="https://tin.test"
    )


async def test_a_week_holds_its_runs_and_its_upcoming_slots(publication_db):
    f = await fixture(publication_db)
    workflow = await digest_workflow(f)
    configured = await f.db.create_project_workflow(
        project_id=f.project.id,
        workflow_id=workflow.id,
        definition_commit_sha=workflow.current_commit_sha,
        name="Weekly digest",
        inputs={"brief": "Summarize the public docs"},
        input_schema=workflow.definition["input_schema"],
        schedule={
            "cadence": "weekly",
            "weekdays": ["thursday"],
            "local_time": "10:00",
            "timezone": LA,
        },
        request_id=uuid4(),
        created_by_clerk_user_id=ACTOR,
        pinned_definition=workflow.definition,
    )
    # Monday 23:30 in Los Angeles is already Tuesday in UTC, and still in this week.
    inside = await run_at(f, workflow, datetime(2030, 1, 8, 7, 30, tzinfo=UTC))
    # A superseded version is not shown, nor a run from Sunday 23:59 in Los Angeles.
    await run_at(f, workflow, datetime(2030, 1, 8, 8, 0, tzinfo=UTC), status="superseded")
    await run_at(f, workflow, datetime(2030, 1, 7, 7, 59, tzinfo=UTC))

    async with client(f) as http:
        response = await http.get(
            f"/api/projects/{f.project.id}/week", params={"start": WEEK, "timezone": LA}
        )
    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["start"] == WEEK and body["timezone"] == LA
    assert [run["id"] for run in body["runs"]] == [str(inside.id)]
    assert body["runs"][0]["workflow_key"] == "custom.research_digest"
    assert body["runs"][0]["status"] == "succeeded"
    [slot] = body["occurrences"]
    assert slot["project_workflow_id"] == str(configured.id)
    assert slot["name"] == "Weekly digest"
    assert slot["state"] == "planned"
    assert slot["lands"] == "Files"
    assert slot["at"].startswith("2030-01-10T18:00:00")


async def test_the_week_needs_project_access_and_a_real_time_zone(publication_db):
    f = await fixture(publication_db)
    await digest_workflow(f)
    async with client(f) as http:
        bad = await http.get(
            f"/api/projects/{f.project.id}/week", params={"start": WEEK, "timezone": "Mars/Base"}
        )
    assert bad.status_code == 422
    async with client(f, actor="user_outsider") as http:
        outside = await http.get(f"/api/projects/{f.project.id}/week", params={"start": WEEK})
    assert outside.status_code in {403, 404}
