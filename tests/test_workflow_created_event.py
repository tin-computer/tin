"""Activation: saving a project's first workflow sends one project_workflow_created event."""

from uuid import uuid4

from test_procedure_publication import publication_db as publication_db
from test_workflow_prerequisites import ACTOR, A, project_fixture

from tin_lite import analytics


async def save(f, key, *, request_id, schedule=None):
    workflow = f.workflows[key]
    return await f.db.create_project_workflow(
        project_id=f.project.id,
        workflow_id=workflow.id,
        definition_commit_sha=A,
        name=f"Saved {key}",
        inputs={},
        input_schema=workflow.definition["input_schema"],
        schedule=schedule,
        request_id=request_id,
        created_by_clerk_user_id=ACTOR,
    )


async def test_the_first_save_is_marked_and_a_retried_save_sends_nothing(
    publication_db, monkeypatch
):
    f = await project_fixture(publication_db)
    events = []
    monkeypatch.setattr(
        analytics, "capture", lambda event, **kwargs: events.append((event, kwargs))
    )
    keys = list(f.workflows)
    first_request = uuid4()
    first = await save(f, keys[0], request_id=first_request)
    await save(f, keys[0], request_id=first_request)  # the same request, retried
    second = await save(f, keys[1], request_id=uuid4())

    assert [event for event, _ in events] == ["project_workflow_created"] * 2
    (_, one), (_, two) = events
    # The person who saved it, so the activation step joins the sign-up funnel by person.
    assert one["distinct_id"] == ACTOR and one["project_id"] == f.project.id
    assert one["properties"] == {
        "project_workflow_id": str(first.id),
        "workflow": keys[0],
        "scheduled": False,
        "cadence": None,
        "first_for_project": True,
    }
    assert two["properties"]["project_workflow_id"] == str(second.id)
    assert two["properties"]["first_for_project"] is False


async def test_the_event_properties_survive_the_analytics_allowlist(publication_db, monkeypatch):
    f = await project_fixture(publication_db)
    client = analytics.Analytics("synthetic", "https://unused.invalid")
    monkeypatch.setattr(client, "_ensure_flusher", lambda: None)
    monkeypatch.setattr(analytics, "_current", client)
    key = next(iter(f.workflows))
    saved = await save(
        f,
        key,
        request_id=uuid4(),
        schedule={
            "cadence": "weekly",
            "weekdays": ["monday"],
            "local_time": "09:00",
            "timezone": "UTC",
        },
    )
    (queued,) = [item for item in client._queue if item["event"] == "project_workflow_created"]
    assert queued["distinct_id"] == ACTOR
    properties = queued["properties"]
    assert properties["project_workflow_id"] == str(saved.id)
    assert properties["workflow"] == key
    assert properties["scheduled"] is True
    assert properties["cadence"] == "weekly"
    assert properties["first_for_project"] is True
