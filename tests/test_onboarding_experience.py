"""Onboarding handoffs use project facts rather than optimistic completion prose."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from test_growth_onboarding_picks import call, harness, picks_args, refused
from test_procedure_publication import publication_db as publication_db

from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.domain import RunStatus
from tin_lite.growth_onboarding_activities import GrowthOnboardingActivities
from tin_lite.onboarding_experience import onboarding_experience, result_links, validate_plan

DEFINITIONS = {w.key: w.definition for w in BUILTIN_WORKFLOWS}
SETTINGS = SimpleNamespace(
    switchboard_public_url="https://api.example.test", app_url="https://app.example.test"
)


def template(key):
    return SimpleNamespace(definition=DEFINITIONS[key], title=key)


def db_fixture():
    return SimpleNamespace(
        list_integration_connections=AsyncMock(return_value=[]),
        get_registry_workflow=AsyncMock(side_effect=lambda key: template(key)),
        get_effect=AsyncMock(return_value=None),
        list_project_workflows=AsyncMock(return_value=[]),
        get_run=AsyncMock(return_value=None),
    )


def run_fixture(project_id, **overrides):
    return SimpleNamespace(
        id=uuid4(),
        project_id=project_id,
        input={"product_url": "https://example.test"},
        status=RunStatus.SUCCEEDED,
        workflow_name="growth.onboarding",
        artifact_path=None,
        canonical_commit_sha=None,
        review_required=False,
        **overrides,
    )


async def test_first_visit_explains_access_and_honest_delivery_without_effects():
    db = db_fixture()
    view = await onboarding_experience(
        database=db, storage=None, settings=SETTINGS, project_id=uuid4()
    )
    assert view["setup_status"] == "not_started"
    assert {n["provider"] for n in view["access_needs"]} == {"infra.github", "analytics.gsc"}
    assert all(
        n["benefit"] and n["permissions"] and n["resource_selection"] for n in view["access_needs"]
    )
    assert view["delivery_destination"]["supported_channels"] == ["tin"]
    assert view["delivery_destination"]["notifications_enabled"] is False
    assert view["first_deliverables"][0]["purpose"]
    assert not view["result_links"] and not view["incomplete_setup"]
    db.get_run.assert_not_called()


def plan(workflow, **inputs):
    block = {
        "systems": [
            {
                "id": "progress",
                "workflows": [
                    {"key": workflow, "mode": "weekly", "weekdays": ["friday"], "inputs": inputs},
                ],
            }
        ]
    }
    return "```tin-plan\n" + json.dumps(block) + "\n```"


async def test_oversized_focus_has_actionable_error_without_echoing_the_input():
    issues = await validate_plan(
        database=db_fixture(),
        project_id=uuid4(),
        text=plan("project.weekly_brief", focus="private business detail " * 30),
        systems=["progress"],
        timezone="America/Los_Angeles",
    )
    assert issues[0]["workflow_key"] == "project.weekly_brief"
    assert issues[0]["reason"] == "focus must satisfy maxLength: 240."
    assert "private business" not in str(issues)
    assert not await validate_plan(
        database=db_fixture(),
        project_id=uuid4(),
        text=plan("project.weekly_brief", focus="Activation and the next useful experiment"),
        systems=["progress"],
        timezone="America/Los_Angeles",
    )


async def test_invalid_enum_and_schedule_are_not_silently_changed():
    issues = await validate_plan(
        database=db_fixture(),
        project_id=uuid4(),
        text=plan("content.public_article", brief="A synthetic guide", goal="get more customers"),
        systems=["progress"],
        timezone="UTC",
    )
    assert issues[0]["code"] == "invalid_inputs"
    issues = await validate_plan(
        database=db_fixture(),
        project_id=uuid4(),
        text=plan("project.weekly_brief"),
        systems=["progress"],
        timezone="not/a/timezone",
    )
    assert issues[0]["code"] == "invalid_schedule"


@pytest.mark.parametrize(
    "workflows",
    [
        None,
        42,
        "brief",
        {"key": "project.weekly_brief"},
        [None],
        [{"key": "project.weekly_brief", "mode": "weekly", "weekdays": [{"day": "friday"}]}],
        [{"key": "project.weekly_brief", "inputs": "private input"}],
    ],
)
async def test_malformed_plan_remains_readable_and_cannot_be_approved(
    publication_db, monkeypatch, workflows
):
    block = {"systems": [{"id": "progress", "workflows": workflows}]}
    text = (
        "# Plan\n## What Tin would run\n- [ ] progress **Progress**\n"
        "## Control\n- [ ] control: review_in_tin\n```tin-plan\n" + json.dumps(block) + "\n```"
    )
    h = await harness(publication_db, monkeypatch, plan=text)
    await call(h, "record_onboarding_picks", **picks_args(h, systems=["progress"], connections=[]))
    view = await call(h, "get_run", run_id=str(h.run.id))
    assert view["incomplete_setup"][0]["code"] == "invalid_plan"
    assert "private input" not in str(view["incomplete_setup"])
    assert "invalid_plan" in await refused(h, "approve_workflow_run", run_id=str(h.run.id))
    h.handle.signal.assert_not_called()
    assert await h.db.get_effect(f"onboarding:{h.run.id}:approved_plan") is None
    assert await h.db.list_project_workflows(project_id=h.run.project_id) == []


@pytest.mark.parametrize("current", ["on_demand", "paused", "removed", "rescheduled"])
async def test_handoff_never_resurrects_a_historical_schedule(current):
    db, project_id, schedule_id = db_fixture(), uuid4(), uuid4()
    old_schedule = {
        "cadence": "weekly",
        "weekdays": ["friday"],
        "local_time": "09:00",
        "timezone": "UTC",
    }
    new_schedule = {**old_schedule, "weekdays": ["monday"]}
    next_run = datetime(2026, 10, 5, 9, tzinfo=UTC) if current == "rescheduled" else None
    if current != "removed":
        db.list_project_workflows.return_value = [
            SimpleNamespace(
                id=schedule_id,
                status="paused" if current == "paused" else "active",
                schedule=None if current == "on_demand" else new_schedule,
                next_run_at=next_run,
            )
        ]
    db.get_effect.return_value = SimpleNamespace(
        status="completed",
        result={
            "actions": [
                {
                    "system": "progress",
                    "key": "project.weekly_brief",
                    "mode": "weekly",
                    "status": "scheduled",
                    "project_workflow_id": str(schedule_id),
                    "schedule": old_schedule,
                    "next_run_at": "2026-09-25T09:00:00+00:00",
                }
            ]
        },
    )
    view = await onboarding_experience(
        database=db,
        storage=None,
        settings=SETTINGS,
        project_id=project_id,
        run=run_fixture(project_id),
    )
    deliverable = view["first_deliverables"][0]
    assert deliverable["schedule"] == (
        None if current in {"on_demand", "removed"} else new_schedule
    )
    assert deliverable["next_run_at"] == (next_run.isoformat() if next_run else None)
    assert (
        deliverable["status"]
        == {
            "on_demand": "on_demand",
            "paused": "schedule_inactive",
            "removed": "schedule_inactive",
            "rescheduled": "scheduled",
        }[current]
    )
    if current in {"paused", "removed"}:
        assert view["setup_status"] == "partial"
        assert any(i["code"] == "schedule_inactive" for i in view["incomplete_setup"])
    elif current == "on_demand":
        assert deliverable["next_action"] == "Start the saved workflow when you need a result"


async def test_setup_reconciles_schedules_children_and_actual_result_links():
    db, project_id = db_fixture(), uuid4()
    parent, audit_id, schedule_id = run_fixture(project_id), uuid4(), uuid4()
    audit = SimpleNamespace(
        id=audit_id,
        project_id=project_id,
        status=RunStatus.SUCCEEDED,
        workflow_name="visibility.audit",
        artifact_path="reports/AI_VISIBILITY.md",
        canonical_commit_sha="a" * 40,
        review_required=False,
    )
    db.get_run.return_value = audit
    db.list_project_workflows.return_value = [SimpleNamespace(id=schedule_id, status="active")]
    db.get_effect.return_value = SimpleNamespace(
        status="completed",
        result={
            "actions": [
                {
                    "system": "visibility",
                    "key": "visibility.audit",
                    "mode": "weekly",
                    "status": "scheduled",
                    "project_workflow_id": str(schedule_id),
                    "run_id": str(audit_id),
                },
                {
                    "system": "progress",
                    "key": "project.weekly_brief",
                    "mode": "weekly",
                    "status": "blocked",
                    "reason": "Invalid focus",
                },
            ]
        },
    )
    view = await onboarding_experience(
        database=db, storage=None, settings=SETTINGS, project_id=project_id, run=parent
    )
    assert view["setup_status"] == "partial"
    assert len(view["incomplete_setup"]) == 1
    assert view["incomplete_setup"][0]["workflow_key"] == "project.weekly_brief"
    assert view["first_deliverables"][0]["status"] == "succeeded"
    db.list_project_workflows.return_value[0].schedule = {
        "cadence": "weekly",
        "weekdays": ["thursday"],
        "local_time": "11:00",
        "timezone": "UTC",
    }
    current = await onboarding_experience(
        database=db, storage=None, settings=SETTINGS, project_id=project_id, run=parent
    )
    assert current["first_deliverables"][0]["schedule"]["weekdays"] == ["thursday"]
    assert (
        view["result_links"][0]["url"]
        == f"https://app.example.test/document/{audit_id}?project={project_id}"
    )
    db.list_project_workflows.return_value = []
    view = await onboarding_experience(
        database=db, storage=None, settings=SETTINGS, project_id=project_id, run=parent
    )
    assert any(i["code"] == "schedule_inactive" for i in view["incomplete_setup"])
    audit.project_id = uuid4()
    with pytest.raises(ValueError, match="outside this project"):
        await onboarding_experience(
            database=db, storage=None, settings=SETTINGS, project_id=project_id, run=parent
        )


async def test_scheduled_first_run_failure_is_not_called_complete():
    db, project_id = db_fixture(), uuid4()
    parent, schedule_id = run_fixture(project_id), uuid4()
    db.list_project_workflows.return_value = [SimpleNamespace(id=schedule_id, status="active")]
    db.get_effect.return_value = SimpleNamespace(
        status="completed",
        result={
            "actions": [
                {
                    "system": "articles",
                    "key": "content.public_article",
                    "mode": "weekly",
                    "status": "scheduled",
                    "project_workflow_id": str(schedule_id),
                    "first_run_status": "blocked",
                    "reason": "Prerequisite missing",
                    "delivery_error": "Draft delivery was not configured",
                },
            ]
        },
    )
    view = await onboarding_experience(
        database=db, storage=None, settings=SETTINGS, project_id=project_id, run=parent
    )
    assert view["setup_status"] == "partial"
    assert {i["code"] for i in view["incomplete_setup"]} == {
        "first_run_blocked",
        "delivery_not_configured",
    }
    assert not view["result_links"]
    assert {n["provider"] for n in view["access_needs"]} == {"infra.github", "analytics.gsc"}


def test_draft_links_point_to_review_and_do_not_invent_unwritten_artifacts():
    run = run_fixture(uuid4())
    assert result_links(SETTINGS, run) == []
    run.status, run.review_required = RunStatus.NEEDS_INPUT, True
    run.artifact_path, run.canonical_commit_sha = "reports/ANSWER_PAGE.md", "b" * 40
    assert result_links(SETTINGS, run)[0]["kind"] == "review"


async def test_mcp_exposes_handoff_and_refuses_invalid_setup_before_approval(
    publication_db, monkeypatch
):
    text = (
        "# Plan\n## What Tin would run\n- [ ] progress **Progress**\n"
        "## Control\n- [ ] control: review_in_tin\n" + plan("project.weekly_brief", focus="x" * 241)
    )
    h = await harness(publication_db, monkeypatch, plan=text)
    await call(h, "record_onboarding_picks", **picks_args(h, systems=["progress"], connections=[]))
    view = await call(h, "get_run", run_id=str(h.run.id))
    assert view["incomplete_setup"][0]["code"] == "invalid_inputs"
    assert view["first_deliverables"][0]["workflow_key"] == "project.weekly_brief"
    assert view["result_links"][0]["url"].endswith(
        f"/document/{h.run.id}?project={h.run.project_id}"
    )
    assert "invalid_plan" in await refused(h, "approve_workflow_run", run_id=str(h.run.id))
    h.handle.signal.assert_not_called()
    assert await h.db.get_effect(f"onboarding:{h.run.id}:approved_plan") is None
    assert await h.db.list_project_workflows(project_id=h.run.project_id) == []


async def test_recommended_access_omitted_from_legacy_plan_can_be_recorded(
    publication_db, monkeypatch
):
    text = (
        "# Plan\n## What Tin would run\n- [ ] progress **Articles**\n"
        "## Control\n- [ ] control: review_in_tin\n"
        + plan("content.public_article", brief="One synthetic guide")
    )
    h = await harness(publication_db, monkeypatch, plan=text)
    result = await call(
        h,
        "record_onboarding_picks",
        **picks_args(
            h,
            systems=["progress"],
            connections=[
                {"provider": "infra.github", "decision": "not_now", "reason": "Connect next week"},
            ],
        ),
    )
    assert result["connections"]["infra.github"] == {
        "state": "declined",
        "note": "Connect next week",
    }
    assert result["access_needs"][0]["decision"] == "declined"
    assert not result["connection_batch"]["arguments"]["providers"]


async def test_partial_delivery_configuration_records_each_actual_destination(monkeypatch):
    db = db_fixture()
    db.get_integration_connection = AsyncMock(
        return_value=SimpleNamespace(
            status="connected",
            configuration={"selected_repository": "example/site"},
        )
    )
    delivery = SimpleNamespace(
        save_settings=AsyncMock(
            side_effect=[
                ValueError("private provider payload must not appear in the report"),
                {"revision": "b" * 40},
            ]
        )
    )
    monkeypatch.setattr(
        "tin_lite.growth_onboarding_activities.ContentDelivery", lambda **kw: delivery
    )
    activities = GrowthOnboardingActivities(
        database=db, storage=None, settings=SETTINGS, integrations=None
    )
    run = SimpleNamespace(id=uuid4(), project_id=uuid4(), started_by_clerk_user_id="synthetic_user")
    outcomes = [
        {"status": "scheduled", "key": key, "project_workflow_id": str(uuid4())}
        for key in ["content.answer_page", "content.public_article"]
    ]
    result = await activities._configure_delivery(run, outcomes, "a" * 40)
    assert result["mode"] == "partial"
    assert outcomes[0]["delivery_mode"] == "draft_only"
    assert outcomes[0]["delivery_error"]
    assert "private provider payload" not in str(outcomes)
    assert outcomes[1]["delivery_mode"] == "github_pr"
    assert result["configured_programs"] == [outcomes[1]["project_workflow_id"]]
