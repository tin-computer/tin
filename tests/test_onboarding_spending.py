"""The onboarding handoff warns when saved schedules may not fit the project's limits."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_billing import ACTOR
from test_billing import billed as billed
from test_procedure_publication import publication_db as publication_db
from test_service_billing import install

from tin_lite.billing_contracts import NANOS_PER_DOLLAR, ProjectSpendingPolicy
from tin_lite.growth_onboarding import founder_words, runs_per_month, spending_warnings
from tin_lite.growth_onboarding_activities import GrowthOnboardingActivities
from tin_lite.workflow_inputs import normalize_workflow_inputs

DOLLAR = NANOS_PER_DOLLAR
# Hosted defaults (migration 047): $10 per run, $10 a month, $10 per scheduled run.
HOSTED = {
    "per_run_nanos": 10 * DOLLAR,
    "monthly_nanos": 10 * DOLLAR,
    "schedule_max_nanos": 10 * DOLLAR,
}
ARTICLE = {
    "title": "Weekly article — https://example.com/",
    "runs": 5,
    "maximum_nanos": 5 * DOLLAR,
    "estimate_nanos": 5 * DOLLAR,
}
ADMISSION = (
    "Tin starts a run only if this month's charges plus that run's maximum fit the limit "
    "(a run still going counts at its maximum), so later runs in a month may not start. To "
    "keep every run, raise the monthly limit with set_project_spending_limits or on the "
    "Billing page."
)
WARNING = (
    "Spending limit: Weekly article — https://example.com/ can run up to 5 times a month at up "
    "to $5.00 a run, up to $25.00 a month in all, and this project's monthly limit is $10.00. "
    + ADMISSION
)


def test_weekly_articles_at_hosted_defaults_name_the_schedule_limit_and_call():
    assert spending_warnings([ARTICLE], HOSTED) == [WARNING]


def test_schedules_that_fit_or_cost_nothing_say_nothing():
    daily = {"title": "Daily check", "runs": 31, "maximum_nanos": DOLLAR // 10}
    assert spending_warnings([{**daily, "estimate_nanos": DOLLAR // 10}], HOSTED) == []
    assert spending_warnings([{**ARTICLE, "maximum_nanos": 0, "estimate_nanos": 0}], HOSTED) == []
    assert spending_warnings([ARTICLE], {**HOSTED, "monthly_nanos": 25 * DOLLAR}) == []
    assert spending_warnings([ARTICLE], None) == []


def test_a_schedule_that_cannot_start_says_which_limit_stops_it():
    (unfunded,) = spending_warnings([ARTICLE], {**HOSTED, "schedule_max_nanos": None})
    assert unfunded == (
        "Spending limit: Weekly article — https://example.com/ will not run on its schedule. "
        "Each run can cost up to $5.00, and this project allows no paid scheduled runs. To run "
        "it, set a limit for scheduled runs with set_project_spending_limits or on the Billing "
        "page."
    )
    (per_run,) = spending_warnings([ARTICLE], {**HOSTED, "per_run_nanos": 4 * DOLLAR})
    assert "this project's per-run limit is $4.00. To run it, raise the per-run limit" in per_run


def test_runs_per_month_counts_the_most_a_calendar_month_holds():
    assert runs_per_month({"cadence": "weekly", "weekdays": ["tuesday"]}) == 5
    assert runs_per_month({"cadence": "weekly", "weekdays": ["monday", "thursday"]}) == 10
    assert runs_per_month({"cadence": "daily", "weekdays": []}) == 31


def test_the_warning_is_a_relay_fact_not_tins_quote():
    setup = {
        "business": "Example",
        "actions": [],
        "links": {"my_system": "m", "decisions": "d", "files": "f"},
        "spending_warnings": [WARNING],
    }
    words = founder_words(setup, titles={})
    assert WARNING in words["relay"] and WARNING not in words["quote"]
    assert words["relay"][-1].startswith("Tell me anything you do by hand")


async def test_report_warns_for_saved_and_upcoming_weekly_articles(billed, monkeypatch):
    f = billed
    f.settings.codex_api_projects = {f.project.id}
    await f.billing.update_policy(
        f.project.id,
        ACTOR,
        ProjectSpendingPolicy(
            per_run_nanos=10 * DOLLAR,
            monthly_nanos=10 * DOLLAR,
            concurrency=1,
            schedule_max_nanos=10 * DOLLAR,
            expected_revision=1,
        ),
    )
    generate = await install(f, "content.generate")
    await install(f, "organic.traffic_system")
    schema = generate.definition["input_schema"]
    await f.db.create_project_workflow(
        project_id=f.project.id,
        workflow_id=generate.id,
        definition_commit_sha="c" * 40,
        name="Weekly article — https://example.com/",
        inputs=normalize_workflow_inputs(
            schema=schema, project_id=f.project.id, inputs={"program_id": str(uuid4())}
        ),
        input_schema=schema,
        schedule={
            "cadence": "weekly",
            "weekdays": ["tuesday"],
            "local_time": "10:00",
            "timezone": "UTC",
        },
        request_id=uuid4(),
        created_by_clerk_user_id=ACTOR,
    )
    # An organic traffic system started by this setup saves its weekly articles later.
    system = SimpleNamespace(
        id=uuid4(),
        input={"site_url": "https://blog.example/", "article_weekdays": ["monday", "thursday"]},
    )
    get_run = f.db.get_run

    async def run_or_system(run_id, **kwargs):
        return system if run_id == system.id else await get_run(run_id, **kwargs)

    monkeypatch.setattr(f.db, "get_run", run_or_system)
    activities = GrowthOnboardingActivities(
        database=f.db, storage=f.storage, settings=f.settings, integrations=None
    )
    run = SimpleNamespace(id=uuid4(), project_id=f.project.id)
    setup = {
        "actions": [
            {"key": "organic.traffic_system", "status": "started", "run_id": str(system.id)}
        ]
    }
    warning = (
        "Spending limit: Weekly article — https://example.com/ can run up to 5 times a month at "
        "up to $5.00 a run and Weekly article — https://blog.example/ can run up to 10 times a "
        "month at up to $5.00 a run, up to $75.00 a month in all, and this project's monthly "
        "limit is $10.00. " + ADMISSION
    )
    assert await activities._spending_warnings(run, setup) == [warning]
    # Saved once: a retried report reads the same words even after the limit changes.
    await f.billing.update_policy(
        f.project.id,
        ACTOR,
        ProjectSpendingPolicy(
            per_run_nanos=10 * DOLLAR,
            monthly_nanos=100 * DOLLAR,
            concurrency=1,
            schedule_max_nanos=10 * DOLLAR,
            expected_revision=2,
        ),
    )
    assert await activities._spending_warnings(run, setup) == [warning]
    fresh = SimpleNamespace(id=uuid4(), project_id=f.project.id)
    assert await activities._spending_warnings(fresh, setup) == []


@pytest.mark.parametrize("enrolled", [False, True])
async def test_no_billing_policy_means_no_warning(billed, enrolled):
    f = billed
    if not enrolled:
        await f.db.pool.execute(
            "UPDATE billing_accounts SET run_billing_enabled=false WHERE workspace_id=$1",
            f.project.workspace_id,
        )
    else:
        await f.db.pool.execute(
            "DELETE FROM billing_project_policies WHERE project_id=$1", f.project.id
        )
    activities = GrowthOnboardingActivities(
        database=f.db, storage=f.storage, settings=f.settings, integrations=None
    )
    run = SimpleNamespace(id=uuid4(), project_id=f.project.id)
    assert await activities._spending_warnings(run, {"actions": []}) == []
