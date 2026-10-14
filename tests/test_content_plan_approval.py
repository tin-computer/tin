"""content.plan 1.1.0: a program's first plan waits in Decisions before anything drafts from it.

Offline: the planning agent's run is faked with a fixture portfolio; Postgres is a disposable
schema. No model, sandbox or provider is called.
"""

import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from test_content_plan_agent import AgentStorage, document, portfolio
from test_content_programs import setup
from test_procedure_publication import publication_db as publication_db

from tin_lite import content_plan as legacy
from tin_lite import content_plan_agent as agent
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.content_draft_sources import ContentDraftSources
from tin_lite.content_programs import ContentPrograms
from tin_lite.organic_audit import canonical_json
from tin_lite.organic_system_activities import OrganicSystemActivities


async def planned(publication_db, monkeypatch):
    """A saved program pinned to content.plan 1.1.0 and a way to run its first plan."""
    db, storage, project, configured, activities, model, create = await setup(
        publication_db, monkeypatch, editorial=True
    )
    spec = next(w for w in BUILTIN_WORKFLOWS if w.key == legacy.KEY)
    assert spec.definition["human_review"]["review_label"] == "Review plan"
    agent_storage = AgentStorage()
    agent_storage.repo = storage.repo
    agent_storage.definition = deepcopy(spec.definition)
    activities.storage = agent_storage
    activities.programs.storage = agent_storage
    await db.pool.execute(
        "UPDATE workflows SET definition=$2::jsonb WHERE id=$1",
        spec.id,
        canonical_json(spec.definition).decode(),
    )
    research = next(w for w in BUILTIN_WORKFLOWS if w.key == agent.RESEARCH_KEY)
    await db.upsert_registry_workflow(
        workflow_id=research.id,
        key=research.key,
        title=research.title,
        description=research.description,
        executor=research.executor,
        definition_repo_id="registry/workflows",
        definition_path=research.definition_path,
        current_commit_sha="d" * 40,
        version_label=research.version_label,
        definition=research.definition,
    )

    async def start(**kwargs):
        child, _ = await db.create_run(
            project_id=kwargs["project_id"],
            workflow_id=research.id,
            input_payload=kwargs["input_payload"],
            start_idempotency_key=kwargs["start_idempotency_key"],
            definition_commit_sha=kwargs["definition_commit_sha"],
        )
        return SimpleNamespace(
            id=child.id, executor="codex.procedure", temporal_workflow_id=f"wf-{child.id}"
        )

    monkeypatch.setattr("tin_lite.run_service.start_workflow_run", start)

    async def first_plan():
        run = await create()
        child = await activities.content_plan_research(str(run.id))
        output = agent.path_template(child["run_id"])
        value = portfolio(8)
        value["opportunities"][0]["title"] = "Linear alternatives for API teams"
        revision = agent_storage.repo.edit({output: document(value).encode()})
        await db.pool.execute(
            "UPDATE workflow_runs SET status='succeeded', canonical_commit_sha=$2, "
            "artifact_path=$3, finished_at=now(), lease_active=false WHERE id=$1",
            UUID(child["run_id"]),
            revision,
            output,
        )
        return run, await activities.content_plan_execute(str(run.id))

    return SimpleNamespace(
        db=db,
        storage=agent_storage,
        project=project,
        configured=configured,
        activities=activities,
        create=create,
        first_plan=first_plan,
    )


async def test_the_first_plan_waits_in_decisions_and_nothing_drafts_until_approved(
    publication_db, monkeypatch
):
    f = await planned(publication_db, monkeypatch)
    run, published = await f.first_plan()
    assert published == {"review": True}
    waiting = await f.db.get_run(run.id)
    assert waiting.review_required and waiting.status.value == "needs_input"
    assert waiting.progress_step == "review" and "Waiting for your approval" in (
        waiting.progress_summary
    )
    # The plan is published and opens with which page serves each target.
    plan_md = (
        await f.storage.read_canonical_artifact(
            repo_id=f.project.state_repo_id,
            commit_sha=waiting.canonical_commit_sha,
            path=waiting.artifact_path,
        )
    ).decode()
    assert plan_md.index("## Targets and pages") < plan_md.index("This is planning")
    assert "| Linear | Competitor |" in plan_md
    decisions = await f.db.list_pending_decisions(project_id=f.project.id)
    (decision,) = [d for d in decisions if d["run_id"] == run.id]
    assert decision["title"] == "Review: Content plan for example.com"
    assert "nothing is drafted until you do" in decision["explanation"]
    assert decision["items"][0]["file"] == waiting.artifact_path
    # A retried attempt still waits; it publishes nothing again.
    assert await f.activities.content_plan_execute(str(run.id)) == {"review": True}
    # Drafting names why it waits, before any run or spend.
    sources = ContentDraftSources(database=f.db, storage=f.storage)
    discovery = await sources.discover(project_id=f.project.id, program_id=f.configured.id)
    assert not discovery["next"]["available"]
    assert discovery["next"]["reason"] == (
        "Approve the content plan in Decisions before drafting from it."
    )
    assert not any(item["available"] for item in discovery["items"])
    with pytest.raises(ValueError, match="Approve the content plan in Decisions"):
        await sources.select(
            project_id=f.project.id,
            inputs={"program_id": str(f.configured.id), "item_id": "op001"},
        )
    # The founder approves: the run finishes and drafting may start.
    await f.db.set_review_actor(run_id=run.id, clerk_user_id="user_test")
    await f.activities.content_plan_approved(str(run.id))
    await f.activities.content_plan_approved(str(run.id))  # a retry changes nothing
    approved = await f.db.get_run(run.id)
    assert approved.status.value == "succeeded" and approved.review_decision == "approved"
    assert (
        await ContentPrograms(database=f.db, storage=f.storage).approval_hold(f.configured.id)
        is None
    )
    discovery = await sources.discover(project_id=f.project.id, program_id=f.configured.id)
    assert discovery["next"]["available"], discovery["next"]["reason"]
    evidence = json.loads(
        await f.storage.read_canonical_artifact(
            repo_id=f.project.state_repo_id,
            commit_sha=approved.canonical_commit_sha,
            path=legacy.paths(str(run.id))["evidence.json"],
        )
    )
    targets = evidence["editorial"]["targets"]
    assert {t["kind"] for t in targets["targets"]} >= {"competitor"}
    # A later run of the same program prepares a batch from the approved plan: no review.
    later = await f.create()
    assert not later.review_required


async def test_a_declined_plan_drafts_nothing_and_the_system_does_not_fall_back(
    publication_db, monkeypatch
):
    f = await planned(publication_db, monkeypatch)
    run, _ = await f.first_plan()
    await f.db.decline_review(run_id=run.id, clerk_user_id="user_test", summary="Declined.")
    hold = await ContentPrograms(database=f.db, storage=f.storage).approval_hold(f.configured.id)
    assert hold and "declined" in hold
    discovery = await ContentDraftSources(database=f.db, storage=f.storage).discover(
        project_id=f.project.id, program_id=f.configured.id
    )
    assert discovery["next"]["reason"] == hold
    system = OrganicSystemActivities.__new__(OrganicSystemActivities)
    system.db = f.db
    content = {"status": "stopped", "run_id": str(run.id), "project_workflow_id": None}
    assert await system.plan_declined(content)
    # No saved-plan fallback is consulted for a plan the founder turned down.
    assert await system.content_program(SimpleNamespace(id=run.id), content) is None


async def test_a_revision_preview_of_an_approved_plan_needs_no_plan_review(
    publication_db, monkeypatch
):
    f = await planned(publication_db, monkeypatch)
    run, _ = await f.first_plan()
    await f.activities.content_plan_approved(str(run.id))
    programs = ContentPrograms(database=f.db, storage=f.storage)
    current = await programs.read(project_id=f.project.id, program_id=f.configured.id)
    request_id = uuid4()
    await programs.begin_revision(
        project_id=f.project.id,
        program_id=f.configured.id,
        request_id=request_id,
        expected_revision=current["revision"],
        batch_ids=["week_20"],
        instruction="Swap week 20 for an integration page.",
        context_paths=[],
        actor="user_test",
    )
    # A revision preview has its own apply step; it never waits for plan approval.
    preview = await f.create(amendment_id=request_id)
    assert not preview.review_required


async def revise(f, monkeypatch, run, feedback="Give the iMessage API group its own page."):
    """Request changes on the waiting plan through the shared review service."""
    from tin_lite.content_plan_feedback import PlanFeedback
    from tin_lite.workflow_reviews import WorkflowReviews

    async def start(**kwargs):
        workflow = kwargs["workflow"]
        child, _ = await f.db.create_run(
            project_id=kwargs["project_id"],
            workflow_id=workflow.id,
            input_payload=kwargs["input_payload"],
            start_idempotency_key=kwargs["start_idempotency_key"],
            definition_commit_sha=kwargs["definition_commit_sha"],
            project_workflow_id=kwargs.get("project_workflow_id"),
            started_by_clerk_user_id=kwargs.get("started_by_clerk_user_id"),
        )
        if workflow.key == agent.RESEARCH_KEY:
            return SimpleNamespace(
                id=child.id, executor="codex.procedure", temporal_workflow_id=f"wf-{child.id}"
            )
        return child

    monkeypatch.setattr("tin_lite.run_service.start_workflow_run", start)
    await f.db.grant_project_membership(project_id=f.project.id, clerk_user_id="user_test")
    runtime = SimpleNamespace(database=f.db, storage=f.storage)
    reviews = WorkflowReviews(runtime=runtime, settings=SimpleNamespace())
    view = await reviews.view(run.id, "user_test")
    assert view["can_request_changes"] and view["submit_label"] == "Revise plan"
    request_id = uuid4()
    same = await reviews.request_changes(
        run_id=run.id,
        actor="user_test",
        feedback=feedback,
        request_id=request_id,
        token=view["review_token"],
    )
    assert same.id == run.id and same.status.value == "needs_input"
    return reviews, PlanFeedback(runtime, SimpleNamespace()), request_id


async def run_revision(f, revision_run_id, title="iMessage API for agent builders"):
    """The revision run's planning agent writes its portfolio, then the plan executes."""
    child = await f.activities.content_plan_research(str(revision_run_id))
    output = agent.path_template(child["run_id"])
    value = portfolio(8)
    value["opportunities"][0]["title"] = title
    revision = f.storage.repo.edit({output: document(value).encode()})
    await f.db.pool.execute(
        "UPDATE workflow_runs SET status='succeeded', canonical_commit_sha=$2, "
        "artifact_path=$3, finished_at=now(), lease_active=false WHERE id=$1",
        UUID(child["run_id"]),
        revision,
        output,
    )
    return await f.activities.content_plan_execute(str(revision_run_id))


async def test_request_changes_revises_the_waiting_plan_in_place(publication_db, monkeypatch):
    from tin_lite.review_revisions import approval_conflict

    f = await planned(publication_db, monkeypatch)
    run, _ = await f.first_plan()
    reviews, plan_feedback, request_id = await revise(f, monkeypatch, run)
    # Revising: the copy stays readable, approval waits, and a second request is refused.
    view = await reviews.view(run.id, "user_test")
    assert view["pending_run_id"] and not view["can_approve"] and not view["can_request_changes"]
    waiting = await f.db.get_run(run.id)
    assert "revising this plan" in await approval_conflict(f.db, waiting, None)
    programs = ContentPrograms(database=f.db, storage=f.storage)
    pending = (await programs.facts(f.configured.id))["pending_revision"]
    assert pending["id"] == request_id and len(pending["batch_ids"]) == 26
    revision_run_id = view["pending_run_id"]
    # A lost response retried with the same request ID starts nothing new.
    again = await reviews.request_changes(
        run_id=run.id,
        actor="user_test",
        feedback="Give the iMessage API group its own page.",
        request_id=request_id,
        token=view["review_token"],
    )
    assert again.id == run.id
    # The revision runs, then Tin applies it and the same review shows the revised plan.
    await run_revision(f, revision_run_id)
    revised = await f.db.get_run(run.id)
    assert revised.status.value == "needs_input" and revised.review_decision is None
    assert revised.artifact_path == legacy.paths(revision_run_id)["PLAN.md"]
    assert revised.canonical_commit_sha != waiting.canonical_commit_sha
    (decision,) = [
        d
        for d in await f.db.list_pending_decisions(project_id=f.project.id)
        if d["run_id"] == run.id
    ]
    assert decision["items"][0]["file"] == revised.artifact_path
    assert decision["explanation"].startswith("Revised from your feedback.")
    view = await reviews.view(run.id, "user_test")
    assert view["can_approve"] and view["proposal_revisions"]["count"] == 1
    current = await programs.read(project_id=f.project.id, program_id=f.configured.id)
    titles = [i["title"] for b in current["plan"]["batches"] for i in b["items"]]
    assert "iMessage API for agent builders" in titles
    assert (await programs.facts(f.configured.id))["pending_revision"] is None
    assert await approval_conflict(f.db, revised, None) is None
    # Approving binds the revised copy.
    await f.activities.content_plan_approved(str(run.id))
    approved = await f.db.get_run(run.id)
    assert approved.status.value == "succeeded"
    assert approved.artifact_path == revised.artifact_path


async def test_an_edit_during_the_revision_keeps_the_founders_plan(publication_db, monkeypatch):
    f = await planned(publication_db, monkeypatch)
    run, _ = await f.first_plan()
    reviews, _, _ = await revise(f, monkeypatch, run)
    programs = ContentPrograms(database=f.db, storage=f.storage)
    revision_run_id = (await reviews.view(run.id, "user_test"))["pending_run_id"]
    # The founder edits week 1 in the plan editor while Tin re-plans.
    current = await programs.read(project_id=f.project.id, program_id=f.configured.id)
    edited = deepcopy(current["plan"])
    edited["batches"][0]["items"] = edited["batches"][0]["items"][:1]
    from tin_lite.content_plan import plan_path

    f.storage.repo.edit({plan_path(f.configured.id): canonical_json(edited)})
    await run_revision(f, revision_run_id)
    unchanged = await f.db.get_run(run.id)
    assert unchanged.artifact_path == legacy.paths(str(run.id))["PLAN.md"]
    view = await reviews.view(run.id, "user_test")
    assert "kept your edits" in view["conflict"] and view["can_approve"]
    assert (await programs.facts(f.configured.id))["pending_revision"] is None
    kept = await programs.read(project_id=f.project.id, program_id=f.configured.id)
    assert kept["plan"]["batches"][0]["items"] == edited["batches"][0]["items"]


def test_mcp_names_the_revision_route_for_a_waiting_plan():
    from tin_lite.document_handoff import revise

    run = SimpleNamespace(
        id=uuid4(),
        project_id=uuid4(),
        executor="content.plan",
        workflow_id=next(w for w in BUILTIN_WORKFLOWS if w.key == legacy.KEY).id,
        project_workflow_id=uuid4(),
        status=SimpleNamespace(value="needs_input"),
        review_required=True,
        review_decision=None,
        artifact_path="reports/content-plan/x/PLAN.md",
        canonical_commit_sha="a" * 40,
    )
    route = revise(SimpleNamespace(public_url="https://tin.test", app_url=None), run)
    assert route["tool"] == "request_workflow_changes" and route["keeps_review"]
