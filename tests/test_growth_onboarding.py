# ruff: noqa: E501
"""Growth onboarding parent: shared form, pinned plan child, one hold, then Tin sets up."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from temporalio.exceptions import ApplicationError
from test_procedure_publication import activity_fixture
from test_procedure_publication import publication_db as publication_db

from tin_lite import growth_onboarding
from tin_lite.catalog import BUILTIN_WORKFLOWS, PARENT_CHILD_KEYS
from tin_lite.growth_onboarding import (
    apply_plan_picks,
    apply_priority,
    chosen_systems,
    founder_message,
    founder_words,
    picked_actions,
    plan_block,
    plan_connections,
    plan_control,
    plan_picks,
    plan_readiness,
    plan_view,
    render_report,
    report_words,
    systems_details,
    ui_links,
)
from tin_lite.growth_onboarding_activities import (
    GrowthOnboardingActivities,
    coerce_enum_inputs,
    tidy_known_inputs,
)
from tin_lite.workflow_inputs import normalize_workflow_inputs

DEFINITIONS = {row.key: row.definition for row in BUILTIN_WORKFLOWS}

PLAN = """# Growth plan for Example

## Tin's view
Being found by the buyers who ask assistants would help Example most.

As the first phase, Tin can start
- checking each Monday whether assistants mention you
- writing one answer page a week
These are Tin's suggestion; you can take on more, or less.

In the next phase, Tin can
- fix the site's technical gaps, once GitHub is connected

## What Tin would run
Tell your agent, in your words, what Tin should take on.
- [ ] technical-seo **Technical SEO** — Tin will fix what slows the site, as pull requests. Needs: GitHub
- [x] ai-visibility **AI visibility** — weekly audit plus a first pass. Needs: nothing (Tin's suggestion)
- [X] outreach **Outreach desk** — a weekly shortlist of prospects. Needs: Google Workspace

## Control
- [ ] control: pull_request — Tin opens a pull request.
- [x] control: review_in_tin — Tin drafts; you approve.
- [ ] control: auto_publish — not yet.

## Connections
- [x] infra.github — GitHub, required: fixes as pull requests
- [ ] workspace.google — Google Workspace, recommended: outreach — not now: personal inbox stays private
- [ ] analytics.gsc — Search Console, recommended: evidence for the audit

```tin-plan
{"systems": [
  {"id": "technical-seo", "name": "Technical SEO",
   "workflows": [{"key": "site.health_improve", "mode": "once", "inputs": {"site_url": "https://example.com/"}}],
   "integrations": ["infra.github"]},
  {"id": "ai-visibility", "name": "AI visibility", "suggested": true,
   "summary": "Tin measures where AI answers name you and drafts the pages that earn a mention.",
   "outlook": {"week": "a first read of five buyer questions and one answer page draft.", "month": "likely two published answer pages and a second read.", "quarter": "likely the first mentions in AI answers if the pages are published."},
   "workflows": [{"key": "visibility.audit", "mode": "weekly", "weekdays": ["monday"], "local_time": "09:00", "inputs": {"target": "this project"}},
                 {"key": "visibility.audit", "mode": "once", "inputs": {"target": "this project"}},
                 {"key": "organic.audit", "mode": "once", "inputs": {"site_url": "https://example.com", "market": "US"}},
                 {"key": "outreach.email_shortlist", "mode": "weekly", "weekdays": ["tuesday"], "local_time": "09:00", "inputs": {"objective": "warm prospects"}}],
   "integrations": ["workspace.google"]},
  {"id": "outreach", "name": "Outreach desk",
   "workflows": [{"key": "outreach.email_shortlist", "mode": "weekly", "weekdays": ["tuesday"], "local_time": "09:00", "inputs": {"objective": "warm prospects"}}],
   "integrations": ["workspace.google"]}
]}
```
"""


def test_plan_parsers_read_the_pick_and_the_machine_block() -> None:
    offered = ["technical-seo", "ai-visibility", "outreach"]
    assert plan_picks(PLAN) == (["ai-visibility", "outreach"], offered)
    assert chosen_systems(["outreach", "ai-visibility"], offered) == ["ai-visibility", "outreach"]
    assert chosen_systems([], offered) == []
    block = plan_block(PLAN)
    assert block is not None and [s["id"] for s in block["systems"]] == offered
    actions = picked_actions(block, ["ai-visibility", "outreach"])
    # The shortlist appears in two systems with the same schedule; it is set up once.
    assert [(a["system"], a["key"], a["mode"]) for a in actions] == [
        ("ai-visibility", "visibility.audit", "weekly"),
        ("ai-visibility", "visibility.audit", "once"),
        ("ai-visibility", "organic.audit", "once"),
        ("ai-visibility", "outreach.email_shortlist", "weekly"),
    ]
    assert actions[0]["weekdays"] == ["monday"] and actions[0]["local_time"] == "09:00"
    assert plan_connections(PLAN) == {
        "infra.github": {"state": "connected", "note": ""},
        "workspace.google": {"state": "declined", "note": "personal inbox stays private"},
        "analytics.gsc": {"state": "open", "note": ""},
    }
    details = systems_details(block, ["ai-visibility"])
    assert details["names"] == ["AI visibility"] and details["summary"].startswith("Tin measures")
    assert details["outlook"]["quarter"].startswith("likely the first mentions")
    assert systems_details(block, ["nope"])["summary"] == ""
    view = plan_view(PLAN)
    assert view.startswith("Being found by the buyers who ask assistants would help Example most.")
    assert "\n\nAs the first phase, Tin can start\n- checking each Monday" in view
    assert view.endswith("- fix the site's technical gaps, once GitHub is connected")
    assert plan_view("no view") == ""
    assert plan_control(PLAN) == "review_in_tin"
    assert plan_control("## Control\n- [ ] control: pull_request — x") is None
    assert plan_block("no block here") is None
    assert plan_block("```tin-plan\nnot json\n```") is None
    assert plan_picks("nothing") == ([], [])


UNTICKED = (
    PLAN.replace("[x]", "[ ]")
    .replace("[X]", "[ ]")
    .replace(" — not now: personal inbox stays private", "")
)
PICKS = {
    "infra.github": ("connected", ""),
    "workspace.google": ("not_now", "personal inbox stays private"),
}


def test_apply_plan_picks_round_trips_through_the_parsers() -> None:
    assert plan_readiness(UNTICKED)["systems"] == []
    assert plan_readiness(UNTICKED)["suggested"] == ["ai-visibility"]
    ticked = apply_plan_picks(
        UNTICKED, systems=["ai-visibility"], control="review_in_tin", connections=PICKS
    )
    assert ticked == PLAN.replace("- [X] outreach", "- [ ] outreach")
    assert (
        apply_plan_picks(
            UNTICKED, systems=["suggested"], control="review_in_tin", connections=PICKS
        )
        == ticked
    )
    readiness = plan_readiness(ticked)
    assert readiness["systems"] == ["ai-visibility"]
    assert readiness["offered"] == ["technical-seo", "ai-visibility", "outreach"]
    assert readiness["control"] == "review_in_tin"
    assert readiness["connections"] == {
        "infra.github": {"state": "connected", "note": ""},
        "workspace.google": {"state": "declined", "note": "personal inbox stays private"},
        "analytics.gsc": {"state": "open", "note": ""},
    }
    assert plan_block(ticked) == plan_block(PLAN)
    assert plan_view(ticked) == plan_view(PLAN)


def test_apply_plan_picks_unticks_previous_choices() -> None:
    changed = apply_plan_picks(
        PLAN,
        systems=["technical-seo"],
        control="pull_request",
        connections={"workspace.google": ("connected", "")},
    )
    assert plan_picks(changed) == (
        ["technical-seo"],
        ["technical-seo", "ai-visibility", "outreach"],
    )
    assert plan_control(changed) == "pull_request"
    assert plan_connections(changed)["workspace.google"] == {"state": "connected", "note": ""}
    assert "- [x] workspace.google — Google Workspace, recommended: outreach\n" in changed
    assert plan_connections(changed)["infra.github"]["state"] == "connected"


def test_apply_plan_picks_is_idempotent_and_keeps_a_bare_not_now() -> None:
    once = apply_plan_picks(
        UNTICKED, systems=["ai-visibility"], control="review_in_tin", connections=PICKS
    )
    assert (
        apply_plan_picks(
            once, systems=["ai-visibility"], control="review_in_tin", connections=PICKS
        )
        == once
    )
    bare = apply_plan_picks(
        UNTICKED,
        systems=["outreach"],
        control="pull_request",
        connections={"analytics.gsc": ("not_now", "")},
    )
    assert (
        "- [ ] analytics.gsc — Search Console, recommended: evidence for the audit — not now\n"
        in bare
    )
    assert plan_connections(bare)["analytics.gsc"] == {"state": "declined", "note": ""}


def test_apply_plan_picks_rejects_unknown_ids_before_changing_anything() -> None:
    with pytest.raises(ValueError, match="offers technical-seo, ai-visibility, outreach"):
        apply_plan_picks(PLAN, systems=["paid-ads"], control="review_in_tin", connections={})
    with pytest.raises(ValueError, match="Pick at least one system"):
        apply_plan_picks(PLAN, systems=[], control="review_in_tin", connections={})
    with pytest.raises(ValueError, match="offers pull_request, review_in_tin"):
        apply_plan_picks(PLAN, systems=["technical-seo"], control="merge", connections={})
    with pytest.raises(ValueError, match="lists infra.github, workspace.google, analytics.gsc"):
        apply_plan_picks(
            PLAN,
            systems=["technical-seo"],
            control="review_in_tin",
            connections={"crm.hubspot": ("connected", "")},
        )
    with pytest.raises(ValueError, match="must be one of connected, not_now"):
        apply_plan_picks(
            PLAN,
            systems=["technical-seo"],
            control="review_in_tin",
            connections={"infra.github": ("maybe", "")},
        )
    with pytest.raises(ValueError, match="no systems checklist"):
        apply_plan_picks(
            "# Plan\n\n## Control\n- [ ] control: review_in_tin\n",
            systems=["technical-seo"],
            control="review_in_tin",
            connections={},
        )


def test_system_ticks_only_count_inside_the_systems_section() -> None:
    stray = PLAN.replace(
        "## Connections\n", "## Connections\n- [x] stray **A stray line that is not a system**\n"
    )
    assert plan_picks(stray) == (
        ["ai-visibility", "outreach"],
        ["technical-seo", "ai-visibility", "outreach"],
    )
    assert plan_picks("- [x] ai-visibility **AI visibility**\n") == ([], [])


def test_ui_links_point_at_the_project_views() -> None:
    project_id = uuid4()
    links = ui_links("https://lite.tin.computer/", project_id)
    assert links["files"] == f"https://lite.tin.computer/files?project={project_id}"
    assert set(links) == {"overview", "my_system", "activity", "decisions", "files", "integrations"}


def test_report_opens_with_the_handshake_and_names_what_runs() -> None:
    setup = {
        "plan_revision": "d" * 40,
        "systems": ["ai-visibility"],
        "offered": ["technical-seo", "ai-visibility", "outreach"],
        "business": "Example",
        "timezone": "America/Los_Angeles",
        "details": {
            "names": ["AI visibility"],
            "summary": "Tin measures where AI answers name you.",
            "outlook": {
                "week": "a first read.",
                "month": "two pages.",
                "quarter": "first mentions.",
            },
        },
        "control": "review_in_tin",
        "delivery": {
            "mode": "github_pr",
            "repository": "example/site",
            "path_pattern": "content/blog/{slug}.md",
        },
        "links": ui_links("https://lite.tin.computer", uuid4()),
        "actions": [
            {
                "system": "ai-visibility",
                "system_name": "AI visibility",
                "key": "visibility.audit",
                "mode": "weekly",
                "weekdays": ["monday"],
                "local_time": "09:00",
                "status": "scheduled",
                "next_run_at": "2026-09-14T16:00:00+00:00",
            },
            {
                "system": "ai-visibility",
                "system_name": "AI visibility",
                "key": "content.answer_page",
                "mode": "weekly",
                "weekdays": ["tuesday"],
                "local_time": "09:00",
                "status": "scheduled",
                "next_run_at": "2026-09-15T16:00:00+00:00",
            },
            {
                "system": "ai-visibility",
                "system_name": "AI visibility",
                "key": "visibility.audit",
                "mode": "once",
                "status": "started",
                "run_id": "r",
            },
            {
                "system": "ai-visibility",
                "system_name": "AI visibility",
                "key": "organic.audit",
                "mode": "once",
                "status": "blocked",
                "unblock": "tin_operator",
                "reason": "Organic audit requires a configured DataForSEO account.",
            },
            {
                "system": "ai-visibility",
                "system_name": "AI visibility",
                "key": "outreach.email_shortlist",
                "mode": "weekly",
                "status": "declined",
                "provider": "workspace.google",
                "note": "personal inbox stays private",
            },
        ],
    }
    titles = {
        "visibility.audit": "Audit AI visibility",
        "content.answer_page": "Draft an answer page",
    }
    message = founder_message(setup, titles=titles)
    # The win first, then each role as a benefit, then what is already there.
    assert message.startswith(
        "Setup is partial for Example: 2 workflow(s) were left out or need attention."
    )
    assert "2 roles on your calendar, America/Los_Angeles time." in message
    assert "Tin measures where AI answers name you." not in message
    assert (
        "- Monday at 09:00: Audit AI visibility. Lands in Files, reports/AI_VISIBILITY.md."
        in message
    )
    assert (
        "- Tuesday at 09:00: Draft an answer page. Lands in Decisions, as a draft; your yes opens "
        "a pull request in example/site." in message
    )
    assert "Already under way: audit ai visibility (first result in about ten minutes)." in message
    assert (
        "In a week: a first read. In a month: two pages. In three months: first mentions."
        not in message
    )
    assert "Your control: Tin drafts; you approve each item in Decisions" in message
    assert "/system?project=" in message and "/decisions?project=" in message
    # Failures come after the wins, in one honest line each.
    assert message.index("Left out by your choice: outreach.email_shortlist") > message.index(
        "Two pages are yours"
    )
    assert "personal inbox stays private" in message
    assert (
        "Waiting: organic.audit. Organic audit requires a configured DataForSEO account." in message
    )
    assert message.endswith("want to talk through how Tin can help Example grow?")
    assert "expand the scope" not in message and "option" not in message.lower()

    # The same words in the two parts the agent treats apart: Tin's own words to quote (the
    # win, the roles, what is under way) and the facts to relay in the agent's words.
    words = founder_words(setup, titles=titles)
    assert words["quote"].startswith("Setup is partial for Example")
    assert words["quote"].endswith(
        "Already under way: audit ai visibility (first result in about ten minutes)."
    )
    assert "In a week" not in words["quote"] and "Two pages" not in words["quote"]
    assert [item.split(":")[0] for item in words["relay"]] == [
        "Your control",
        "Two pages are yours",
        "Reports arrive in Files (https",
        "Waiting",
        "Left out by your choice",
        "Tell me anything you do by hand for marketing and I will have Tin build it as a "
        "workflow; you will see it appear in My system. Once the first result is in, want to "
        "talk through how Tin can help Example grow?",
    ]
    assert message == "\n\n".join([words["quote"], *words["relay"]])

    text = render_report(setup, titles=titles)
    assert text.startswith("# Tin setup needs attention: AI visibility\n\n" + message)
    assert "## What runs" in text and "## How drafts ship" in text
    assert (
        "Approved drafts open a pull request in example/site under content/blog/{slug}.md" in text
    )
    assert "## Next, for your agent" not in text
    assert f"Plan revision: `{'d' * 40}`." in text
    # The report carries the split in a block Tin reads back for get_run.
    assert report_words(text) == words
    # A report written before the block carries the handshake as one text; it is the quote.
    assert report_words("# Tin is set up\n\nOld handshake.\n\n## What runs\n") == {
        "quote": "Old handshake.",
        "relay": [],
    }


def test_priority_fills_hours_budget_and_urgency_unless_known() -> None:
    assert apply_priority({"priority": "fun"}) == {
        "priority": "fun",
        "founder_hours": "min",
        "budget": "none",
        "urgency": "patient",
    }
    assert apply_priority({"priority": "main", "budget": "none", "urgency": "unknown"}) == {
        "priority": "main",
        "budget": "none",
        "founder_hours": "lots",
        "urgency": "weeks",
    }
    assert apply_priority({"priority": "unknown", "founder_hours": "some"}) == {
        "priority": "unknown",
        "founder_hours": "some",
    }


def test_parent_shares_the_plan_form_and_pins_its_child() -> None:
    parent = DEFINITIONS[growth_onboarding.KEY]
    plan = DEFINITIONS["growth.onboarding_plan"]
    assert parent["input_schema"] == plan["input_schema"]
    assert parent["executor"] == growth_onboarding.KEY
    assert parent["system"] == "start-here"
    assert parent["schedule_modes"] == ["on_demand"]
    assert parent["human_review"]["eligible"] is True
    assert parent["human_review"]["review_label"] == "Set it up"
    assert parent["growth_onboarding_policy"] == growth_onboarding.POLICY
    assert PARENT_CHILD_KEYS[growth_onboarding.KEY] == ("growth.onboarding_plan",)
    properties = parent["input_schema"]["properties"]
    assert len([key for key in properties if key.startswith("system_")]) == 11
    for key, choice in growth_onboarding.FOUNDER_CHOICES.items():
        assert properties[key]["enum"] == choice["enum"] and properties[key]["default"] == "unknown"
    assert properties["hard_nos"]["items"]["enum"] == growth_onboarding.HARD_NOS
    assert properties["timezone"]["default"] == "UTC"


async def parent_fixture(db, monkeypatch, plan=PLAN):
    _, storage, run, _ = await activity_fixture(db, review=True)
    inputs = normalize_workflow_inputs(
        schema=growth_onboarding.INPUT_SCHEMA,
        project_id=run.project_id,
        inputs={
            "product_url": "https://example.com/",
            "system_hosting": "Next.js on Vercel",
            "priority": "side",
            "hard_nos": ["no_paid_ads"],
            "timezone": "America/Los_Angeles",
        },
    )
    await db.pool.execute(
        "UPDATE workflows SET key=$2, executor=$2, definition=$3::jsonb WHERE id=$1",
        run.workflow_id,
        growth_onboarding.KEY,
        json.dumps(DEFINITIONS[growth_onboarding.KEY]),
    )
    await db.pool.execute(
        "UPDATE workflow_runs SET executor=$2, input=$3::jsonb, lease_active=false, "
        "started_by_clerk_user_id='user_tester' WHERE id=$1",
        run.id,
        growth_onboarding.KEY,
        json.dumps(inputs),
    )
    for workflow_key in (
        "growth.onboarding_plan",
        "visibility.audit",
        "site.health_improve",
        "organic.audit",
        "outreach.email_shortlist",
    ):
        definition = DEFINITIONS[workflow_key]
        await db.pool.execute(
            "INSERT INTO workflows (id,key,title,executor,definition_repo_id,definition_path,"
            "current_commit_sha,version_label,definition) "
            "VALUES ($1,$2,$2,$3,'registry/workflows',$4,$5,'1',$6::jsonb)",
            uuid4(),
            workflow_key,
            definition["executor"],
            f"workflows/{workflow_key}.json",
            "e" * 40,
            json.dumps(definition),
        )
    monkeypatch.setattr(db, "has_project_access", AsyncMock(return_value=True))

    async def read(**kwargs):
        if kwargs["repo_id"] == "registry/workflows":
            assert kwargs["commit_sha"] == run.definition_commit_sha
            return json.dumps(DEFINITIONS[kwargs["path"][10:-5]]).encode()
        return plan.encode()

    async def head(**kwargs):
        return ([growth_onboarding.PLAN_PATH], "d" * 40)

    monkeypatch.setattr(storage, "read_canonical_artifact", read)
    monkeypatch.setattr(storage, "list_canonical_files", head)
    settings = SimpleNamespace(
        billing_hosted_defaults_enabled=True,
        luna_api_key="fixture",
        temporal_task_queue="fixture",
        task_queue="fixture",
        switchboard_public_url="https://lite.tin.computer",
    )

    class FakeSchedules:
        created: list = []

        def __init__(self, *, client, settings):
            pass

        async def create(self, *, project_workflow_id, schedule):
            FakeSchedules.created.append((project_workflow_id, schedule.cadence, schedule.timezone))
            return f"tin-lite-project-workflow:{project_workflow_id}"

    monkeypatch.setattr(
        "tin_lite.growth_onboarding_activities.TemporalScheduleService", FakeSchedules
    )
    activities = GrowthOnboardingActivities(
        database=db,
        storage=storage,
        settings=settings,
        integrations=SimpleNamespace(),
        temporal=object(),
    )
    return SimpleNamespace(
        db=db, run=await db.get_run(run.id), activities=activities, schedules=FakeSchedules
    )


async def _finish_child(db, child_id, path):
    await db.pool.execute(
        "UPDATE workflow_runs SET status='succeeded', canonical_commit_sha=$2, "
        "artifact_path=$3, artifact_ref=$4 WHERE id=$1",
        child_id,
        "c" * 40,
        path,
        f"code.storage://state@{'c' * 40}/{path}",
    )


async def test_parent_plans_holds_then_sets_up_the_picks(publication_db, monkeypatch):
    f = await parent_fixture(publication_db, monkeypatch)
    run_id = str(f.run.id)
    await f.activities.growth_onboarding_prepare(run_id)

    payload = {"run_id": run_id, "step": "plan"}
    first = await f.activities.growth_onboarding_step(payload)
    assert first == await f.activities.growth_onboarding_step(payload)
    plan_child = await f.db.get_run(UUID(first["run_id"]))
    assert plan_child.definition_commit_sha == f.run.definition_commit_sha
    assert plan_child.input["product_url"] == "https://example.com/"
    # The child receives hours, budget and urgency derived from the priority answer.
    assert plan_child.input["priority"] == "side"
    assert plan_child.input["founder_hours"] == "some"
    assert plan_child.input["budget"] == "under_500"
    assert plan_child.input["urgency"] == "two_months"
    assert plan_child.input["hard_nos"] == ["no_paid_ads"]

    with pytest.raises(ApplicationError):
        await f.activities.growth_onboarding_review(run_id)

    await _finish_child(f.db, plan_child.id, growth_onboarding.PLAN_PATH)
    assert await f.activities.growth_onboarding_review(run_id) is True
    held = await f.db.get_run(f.run.id)
    assert held.status.value == "needs_input"
    assert held.artifact_path == growth_onboarding.PLAN_PATH
    # The review explanation is Tin's view and nothing else: the dashboard shows it to the
    # founder, and the agent quotes it as given. Its own next steps live in get_started.
    explanation = await f.db.pool.fetchval(
        "SELECT explanation FROM run_decisions WHERE id = $1 AND kind = 'review'", f.run.id
    )
    assert explanation == plan_view(PLAN)
    assert "record_onboarding_picks" not in explanation

    from tin_lite.growth_onboarding_control import ensure_onboarding_approvable

    await ensure_onboarding_approvable(
        runtime=SimpleNamespace(database=f.db, storage=f.activities.storage), run=held
    )
    await f.activities.growth_onboarding_approval(run_id)
    assert (await f.db.get_run(f.run.id)).status.value == "running"

    started = await f.activities.growth_onboarding_setup(run_id)
    assert started == await f.activities.growth_onboarding_setup(run_id)
    receipt = await f.db.get_effect(f"onboarding:{run_id}:setup")
    outcomes = {(a["system"], a["key"], a["mode"]): a for a in receipt.result["actions"]}
    # Both ticked systems are set up; the shortlist they share is set up once.
    assert receipt.result["systems"] == ["ai-visibility", "outreach"]
    assert set(outcomes) == {
        ("ai-visibility", "visibility.audit", "weekly"),
        ("ai-visibility", "visibility.audit", "once"),
        ("ai-visibility", "organic.audit", "once"),
        ("ai-visibility", "outreach.email_shortlist", "weekly"),
    }
    # The founder declined the mailbox, so the shortlist is left out by choice, not blocked.
    assert outcomes[("ai-visibility", "outreach.email_shortlist", "weekly")]["status"] == "declined"
    assert (
        outcomes[("ai-visibility", "outreach.email_shortlist", "weekly")]["provider"]
        == "workspace.google"
    )
    assert receipt.result["connections"]["workspace.google"]["state"] == "declined"
    assert receipt.result["details"]["names"] == ["AI visibility", "Outreach desk"]
    assert receipt.result["business"] and receipt.result["delivery"] is None
    assert receipt.result["control"] == "review_in_tin"
    assert outcomes[("ai-visibility", "visibility.audit", "weekly")]["status"] == "scheduled"
    assert f.schedules.created and f.schedules.created[0][1:] == ("weekly", "America/Los_Angeles")
    assert outcomes[("ai-visibility", "visibility.audit", "once")]["status"] == "started"
    # organic.audit is blocked on the operator's DataForSEO settings in this fixture.
    assert outcomes[("ai-visibility", "organic.audit", "once")]["status"] == "blocked"
    assert outcomes[("ai-visibility", "organic.audit", "once")]["unblock"] == "tin_operator"
    # The weekly schedule also starts its first occurrence now, so two children dispatch.
    assert len(started) == 2
    assert all(set(row) == {"run_id", "executor", "temporal_workflow_id"} for row in started)
    assert outcomes[("ai-visibility", "visibility.audit", "weekly")]["run_id"]
    saved = await f.db.pool.fetch("SELECT name, status FROM project_workflows")
    assert len(saved) == 1 and saved[0]["status"] == "active"
    # Plain names: no "onboarding option" suffix.
    assert "onboarding" not in saved[0]["name"].lower()

    assert await f.activities.growth_onboarding_report(run_id) is True
    done = await f.db.get_run(f.run.id)
    assert done.status.value == "succeeded"
    assert done.artifact_path == f"reports/onboarding/{f.run.id}/RESULT.md"


async def test_setup_fails_clearly_when_no_system_is_ticked(publication_db, monkeypatch):
    f = await parent_fixture(publication_db, monkeypatch, plan=UNTICKED)
    run_id = str(f.run.id)
    await f.activities.growth_onboarding_prepare(run_id)
    first = await f.activities.growth_onboarding_step({"run_id": run_id, "step": "plan"})
    await _finish_child(f.db, UUID(first["run_id"]), growth_onboarding.PLAN_PATH)
    assert await f.activities.growth_onboarding_review(run_id) is True
    await f.activities.growth_onboarding_approval(run_id)

    with pytest.raises(ApplicationError, match="no saved approval snapshot") as failure:
        await f.activities.growth_onboarding_setup(run_id)
    assert failure.value.non_retryable
    assert await f.db.pool.fetch("SELECT id FROM project_workflows") == []
    assert await f.db.get_effect(f"onboarding:{run_id}:setup") is None
    run = await f.db.get_run(f.run.id)
    assert run.status.value == "running"


def test_setup_repairs_inputs_the_plan_padded_or_got_wrong() -> None:
    schema = {
        "properties": {
            "focus": {"enum": ["automatic", "accessibility"], "default": "automatic"},
            "market": {"enum": ["US", "GB"]},
            "site_url": {"type": "string"},
        }
    }
    fixed, notes = coerce_enum_inputs(
        schema, {"focus": "fix the slow pages", "market": "Mars", "site_url": "https://x.test"}
    )
    assert fixed == {"focus": "automatic", "site_url": "https://x.test"}
    assert notes == [
        "focus reset to 'automatic'; the plan wrote 'fix the slow pages'",
        "market dropped; the plan wrote 'Mars', not one of the choices",
    ]
    assert coerce_enum_inputs({}, {"a": 1}) == ({"a": 1}, [])

    padded = "with.md — https://with.md; Markdown collaboration for developers and coding agents"
    tidied, tidy_notes = tidy_known_inputs("visibility.audit", {"target": padded}, {})
    assert tidied["target"] == "https://with.md" and tidy_notes
    tidied, _ = tidy_known_inputs(
        "visibility.audit",
        {"target": "Claw Messenger, the iMessage API for agents"},
        {"product_url": "https://clawmessenger.com/"},
    )
    assert tidied["target"] == "https://clawmessenger.com/"
    assert tidy_known_inputs("visibility.audit", {"target": "with.md"}, {}) == (
        {"target": "with.md"},
        [],
    )
    assert tidy_known_inputs("visibility.audit", {"target": "this project"}, {}) == (
        {"target": "this project"},
        [],
    )
    assert tidy_known_inputs("content.answer_page", {"target": padded}, {}) == (
        {"target": padded},
        [],
    )


async def approved_parent(db, monkeypatch):
    from tin_lite.growth_onboarding_control import ensure_onboarding_approvable

    f = await parent_fixture(db, monkeypatch)
    run_id = str(f.run.id)
    await f.activities.growth_onboarding_prepare(run_id)
    child = await f.activities.growth_onboarding_step({"run_id": run_id, "step": "plan"})
    await _finish_child(db, UUID(child["run_id"]), growth_onboarding.PLAN_PATH)
    await f.activities.growth_onboarding_review(run_id)
    await ensure_onboarding_approvable(
        runtime=SimpleNamespace(database=db, storage=f.activities.storage), run=f.run
    )
    await f.activities.growth_onboarding_approval(run_id)
    return f


async def test_setup_uses_approved_snapshot_after_project_head_changes(publication_db, monkeypatch):
    f = await approved_parent(publication_db, monkeypatch)
    later = apply_plan_picks(
        PLAN, systems=["technical-seo"], control="review_in_tin", connections={}
    )

    async def read(**kwargs):
        return later.encode()

    monkeypatch.setattr(f.activities.storage, "read_canonical_artifact", read)
    monkeypatch.setattr(
        f.activities,
        "_start_child",
        AsyncMock(
            return_value={
                "run_id": str(uuid4()),
                "executor": "visibility.audit",
                "temporal_workflow_id": "child",
            }
        ),
    )
    result = await f.activities.growth_onboarding_setup(str(f.run.id))
    replay = await f.activities.growth_onboarding_setup(str(f.run.id))
    assert result == replay
    assert all(set(row) == {"run_id", "executor", "temporal_workflow_id"} for row in replay)
    saved = await f.db.get_effect(f"onboarding:{f.run.id}:setup")
    assert saved.result["systems"] == ["ai-visibility", "outreach"]
    assert saved.result["plan_revision"] == "d" * 40
    assert len(f.schedules.created) == 1


async def test_unknown_first_admission_retries_without_hiding_or_recreating_schedule(
    publication_db, monkeypatch
):
    f = await approved_parent(publication_db, monkeypatch)
    original = f.activities._start_child
    admitted = []

    async def lost_response(*args, **kwargs):
        child = await original(*args, **kwargs)
        admitted.append(child)
        if len(admitted) == 1:
            raise RuntimeError("response lost after admission")
        return child

    monkeypatch.setattr(f.activities, "_start_child", lost_response)
    with pytest.raises(ApplicationError, match="retry will reconcile"):
        await f.activities.growth_onboarding_setup(str(f.run.id))
    assert await f.db.get_effect(f"onboarding:{f.run.id}:setup") is None
    pending = await f.db.get_effect(f"onboarding:{f.run.id}:action:0:enact")
    assert pending.status == "started"
    assert pending.result["first_run_status"] == "pending"
    assert pending.result["schedule"]["project_workflow_id"]
    result = await f.activities.growth_onboarding_setup(str(f.run.id))
    assert admitted[0] == admitted[1]
    assert admitted[0] in result
    assert len(f.schedules.created) == 1
    assert len(await f.db.pool.fetch("SELECT id FROM project_workflows")) == 1
    saved = await f.db.get_effect(f"onboarding:{f.run.id}:setup")
    assert saved.result["actions"][0]["status"] == "scheduled"
    assert saved.result["actions"][0]["first_run_status"] == "admitted"


async def test_definitive_first_admission_refusal_reports_active_schedule(
    publication_db, monkeypatch
):
    from tin_lite.workflow_prerequisites import PrerequisiteError

    f = await approved_parent(publication_db, monkeypatch)
    original = f.activities._start_child

    async def refuse_first(*args, **kwargs):
        if kwargs.get("project_workflow_id"):
            raise PrerequisiteError("prerequisite_missing", "Required evidence is missing.")
        return await original(*args, **kwargs)

    monkeypatch.setattr(f.activities, "_start_child", refuse_first)
    result = await f.activities.growth_onboarding_setup(str(f.run.id))
    assert len(result) == 1
    saved = await f.db.get_effect(f"onboarding:{f.run.id}:setup")
    action = saved.result["actions"][0]
    assert action["status"] == "scheduled" and action["first_run_status"] == "blocked"
    assert "schedule is active, but its first run was not admitted" in render_report(
        saved.result, titles={}
    )


async def test_exhausted_setup_closes_undispatched_runs_and_retains_schedule(
    publication_db, monkeypatch
):
    f = await approved_parent(publication_db, monkeypatch)
    original = f.activities._start_child
    admitted = []

    async def fail_after_admission(*args, **kwargs):
        child = await original(*args, **kwargs)
        admitted.append(child)
        raise RuntimeError("acknowledgement unavailable")

    monkeypatch.setattr(f.activities, "_start_child", fail_after_admission)
    with pytest.raises(ApplicationError, match="retry will reconcile"):
        await f.activities.growth_onboarding_setup(str(f.run.id))
    await f.activities.growth_onboarding_failure(str(f.run.id))
    child = await f.db.get_run(UUID(admitted[0]["run_id"]))
    assert child.status.value == "failed"
    assert "before this run was dispatched" in child.error_message
    assert (await f.db.get_run(f.run.id)).status.value == "failed"
    assert (
        await f.db.pool.fetchval("SELECT count(*) FROM project_workflows WHERE status='active'")
        == 1
    )


async def test_setup_receipts_share_connection_in_a_small_pool(publication_db, monkeypatch):
    import asyncio

    import asyncpg

    f = await approved_parent(publication_db, monkeypatch)
    schema = await f.db.pool.fetchval("SELECT current_schema()")
    await f.db.pool.close()
    f.db._pool = await asyncpg.create_pool(
        f.db._dsn, min_size=1, max_size=2, server_settings={"search_path": schema}
    )
    result = await asyncio.wait_for(f.activities.growth_onboarding_setup(str(f.run.id)), timeout=10)
    assert len(result) == 2
