"""A film's directions wait in Decisions: the founder picks one to build, or asks for others,
and the note's next version is the film. Both are revisions of the same note."""

from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from test_content_draft import fixture as planned_fixture
from test_feature_film import DIRECTIONS, NOTE
from test_private_workflows import ACTOR, app, mcp, structured
from test_procedure_publication import publication_db as publication_db

from tin_lite import film_directions
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.organic_audit import canonical_json
from tin_lite.run_service import start_workflow_run
from tin_lite.workflow_review_store import ReviewConflict
from tin_lite.workflow_reviews import WorkflowReviews, saved_revision_context

PATH = "films/herding.md"


async def setup(db, monkeypatch):
    """Film a feature 1.2.0 installed beside a project that can run it."""
    f = await planned_fixture(db, monkeypatch, clean=True)
    spec = next(w for w in BUILTIN_WORKFLOWS if w.key == "creative.feature_film")
    definition, resources = spec.definition_and_resource_files()
    await db.upsert_registry_workflow(
        workflow_id=spec.id,
        key=spec.key,
        title=spec.title,
        description=spec.description,
        executor=spec.executor,
        definition_repo_id="registry/workflows",
        definition_path=spec.definition_path,
        current_commit_sha="f" * 40,
        version_label=spec.version_label,
        definition=definition,
    )
    read = f.storage.read_canonical_artifact

    async def read_resource(**kw):
        if kw["repo_id"] == "registry/workflows" and kw["commit_sha"] == "f" * 40:
            return (
                canonical_json(definition)
                if kw["path"] == spec.definition_path
                else resources[kw["path"]]
            )
        return await read(**kw)

    f.storage.read_canonical_artifact = read_resource
    # The studio's voice is a configured provider; a film run starts only with it.
    f.settings.fal_key = "synthetic"
    f.film = await db.get_workflow(spec.id)
    f.reviews = WorkflowReviews(runtime=f.runtime, settings=f.settings)
    return f


async def start(f):
    return await start_workflow_run(
        runtime=f.runtime,
        settings=f.settings,
        workflow=f.film,
        project_id=f.project.id,
        started_by_clerk_user_id=ACTOR,
        input_payload={"slug": "herding"},
    )


async def save(f, run, note):
    """The run's note as Tin publishes it, then waiting in Decisions."""
    revision = f.storage.repo.edit({PATH: note})
    key = f"{run.id}:procedure_canonical_commit"
    async with f.db.effect_lock(key, "procedure_canonical_commit") as (conn, _):
        await f.db.start_effect(conn, execution_key=key, operation="procedure_canonical_commit")
        await f.db.complete_effect(
            conn,
            execution_key=key,
            result={"artifact_path": PATH, "canonical_commit_sha": revision},
        )
    await f.db.request_human_review(
        run_id=run.id,
        canonical_commit_sha=revision,
        artifact_path=PATH,
        artifact_ref=f"code.storage://{f.project.state_repo_id}@{revision}/{PATH}",
    )
    return await f.db.get_run(run.id)


async def test_directions_are_picked_from_and_the_film_is_approved(publication_db, monkeypatch):
    f = await setup(publication_db, monkeypatch)
    first = await save(f, await start(f), DIRECTIONS)
    view = await f.reviews.view(first.id, ACTOR)
    # The card reads each sketch by its project path, with the recommended one and why.
    assert view["directions"]["recommended"] == 2
    assert view["directions"]["why"] == "Its frame says the claim without the caption."
    assert [item["still"] for item in view["directions"]["sketches"]] == [
        "films/herding.assets/1-iceberg.jpg",
        "films/herding.assets/2-flow.jpg",
        "films/herding.assets/3-one-person.png",
    ]
    assert view["directions"]["sketches"][0]["page"] == "films/herding.assets/1-iceberg.html"
    assert view["can_request_changes"] and not view["can_approve"]
    assert view["submit_label"] == "Sketch other directions"
    with pytest.raises(ReviewConflict, match="Pick one to build"):
        await f.reviews.approve(run_id=first.id, actor=ACTOR, token=view["review_token"])
    # The decision offers revisions, the way a draft's does.
    decision = await f.db.pool.fetchrow(
        "SELECT feedback_supported FROM run_decisions WHERE run_id=$1", first.id
    )
    assert decision["feedback_supported"]

    built = await film_directions.pick(
        f.reviews,
        run_id=first.id,
        actor=ACTOR,
        token=view["review_token"],
        request_id=uuid4(),
        direction=2,
        note="End on the 96 who fit.",
    )
    assert built.review_source_run_id == first.id and built.review_version == 2
    context = await saved_revision_context(f.db, f.storage, built)
    assert context["feedback"] == "Build direction 2, Flow.\n\nEnd on the 96 who fit."
    assert context["previous_copy"] == DIRECTIONS.decode()
    prompt = film_directions.revision_prompt(context)
    assert "Build direction N, Name." in prompt and "Start from the closest example" in prompt

    # The film arrives at the same path; it is watched and approved, or revised again.
    film = await save(f, built, NOTE)
    watched = await f.reviews.view(film.id, ACTOR)
    assert watched["directions"] is None
    assert watched["can_approve"] and watched["can_request_changes"]
    assert watched["submit_label"] == "Revise film"
    assert watched["root_run_id"] == str(first.id)
    with pytest.raises(ReviewConflict, match="no directions waiting"):
        await film_directions.pick(
            f.reviews,
            run_id=film.id,
            actor=ACTOR,
            token=watched["review_token"],
            request_id=uuid4(),
            direction=1,
        )


async def test_other_directions_and_a_pick_that_isnt_offered(publication_db, monkeypatch):
    f = await setup(publication_db, monkeypatch)
    first = await save(f, await start(f), DIRECTIONS)
    view = await f.reviews.view(first.id, ACTOR)
    for direction, message in ((4, "1 to 3"), (0, "1 to 3")):
        with pytest.raises(ValueError, match=message):
            await film_directions.pick(
                f.reviews,
                run_id=first.id,
                actor=ACTOR,
                token=view["review_token"],
                request_id=uuid4(),
                direction=direction,
            )
    with pytest.raises(ValueError, match="2,000 characters"):
        film_directions.feedback(view["directions"], 1, "x" * 2001)
    request = uuid4()
    again = await film_directions.pick(
        f.reviews,
        run_id=first.id,
        actor=ACTOR,
        token=view["review_token"],
        request_id=request,
        direction=None,
        note="Something with a map.",
    )
    context = await saved_revision_context(f.db, f.storage, again)
    assert context["feedback"] == "Sketch other directions.\n\nSomething with a map."
    # A lost response is retried with the same request: one run, not two.
    replay = await film_directions.pick(
        f.reviews,
        run_id=first.id,
        actor=ACTOR,
        token=view["review_token"],
        request_id=request,
        direction=None,
        note="Something with a map.",
    )
    assert replay.id == again.id


async def test_the_dashboard_and_an_agent_pick_through_one_contract(publication_db, monkeypatch):
    f = await setup(publication_db, monkeypatch)
    first = await save(f, await start(f), DIRECTIONS)
    server = mcp(f, monkeypatch)
    review = structured(await server.call_tool("get_workflow_review", {"run_id": str(first.id)}))
    assert review["directions"]["recommended"] == 2
    result = structured(
        await server.call_tool(
            "pick_film_direction",
            {
                "run_id": str(first.id),
                "review_token": review["review_token"],
                "request_id": str(uuid4()),
                "direction": 1,
            },
        )
    )
    built = await f.db.get_run(UUID(result["run_id"]))
    assert result["version"] == 2 and built.review_source_run_id == first.id
    second = await save(f, built, DIRECTIONS)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app(f)), base_url="https://tin.test"
    ) as client:
        current = (await client.get(f"/api/workflows/runs/{second.id}/review")).json()
        refused = await client.post(
            f"/api/workflows/runs/{second.id}/film-direction",
            json={
                "direction": 7,
                "request_id": str(uuid4()),
                "review_token": current["review_token"],
            },
        )
        assert refused.status_code == 422
        picked = await client.post(
            f"/api/workflows/runs/{second.id}/film-direction",
            json={
                "direction": 3,
                "note": "Keep her name.",
                "request_id": str(uuid4()),
                "review_token": current["review_token"],
            },
        )
        assert picked.status_code == 202, picked.text
        assert (
            await client.post(
                f"/api/workflows/runs/{second.id}/approve",
                json={"review_token": current["review_token"]},
            )
        ).status_code == 409
    run = await f.db.get_run(UUID(picked.json()["id"]))
    context = await saved_revision_context(f.db, f.storage, run)
    assert context["feedback"] == "Build direction 3, One person.\n\nKeep her name."
    assert SimpleNamespace(**context).previous_copy == DIRECTIONS.decode()
