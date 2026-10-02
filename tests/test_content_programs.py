"""Real SQL + changing code.storage model. No paid services or production writes."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_procedure_publication import HistoryStorage
from test_procedure_publication import publication_db as publication_db

from tin_lite import content_plan as legacy
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.code_storage import ProjectStateChangedError
from tin_lite.content_plan import KEY, plan_path
from tin_lite.content_plan_activities import ContentPlanActivities
from tin_lite.content_programs import ContentPrograms
from tin_lite.organic_audit import canonical_json
from tin_lite.project_files import StaleProjectRevisionError
from tin_lite.publication import PublicationPendingError


class Storage(HistoryStorage):
    async def read_canonical_artifact(self, *, repo_id, commit_sha, path):
        if repo_id == "registry/workflows":
            return canonical_json(self.definition)
        return self.repo.trees[commit_sha][path][1]

    async def read_canonical_artifact_if_exists(self, *, repo_id, commit_sha, path):
        return self.repo.trees[commit_sha].get(path, (None, None))[1]

    async def commit_project_changes(
        self, *, repo_id, branch, expected_head_sha, request_id, message, changes
    ):
        if self.repo.head != expected_head_sha:
            raise ProjectStateChangedError("canonical project state changed before file commit")
        sha = self.repo.edit({change.path: change.content.encode() for change in changes}, message)
        return sha, tuple(change.path for change in changes)


async def setup(database, monkeypatch, *, editorial=False):
    storage = Storage()
    spec = next(spec for spec in BUILTIN_WORKFLOWS if spec.key == KEY)
    definition = deepcopy(spec.definition)
    if not editorial:
        definition.update(
            version="0.1.0",
            content_policy=legacy.POLICY,
            content_instructions=legacy.INSTRUCTIONS,
            content_schema=legacy.MODEL_SCHEMA,
        )
        definition["model_route"]["key"] = legacy.ROUTE_KEY
    storage.definition = definition
    project = await database.create_project(name="Content proof", state_repo_id=storage.repo.id)
    await database.pool.execute(
        """INSERT INTO workflows (id,key,title,executor,definition_repo_id,
        definition_path,current_commit_sha,version_label,definition)
        VALUES ($1,$2,$3,$2,'registry/workflows',
                'workflows/content.plan.json',$4,'0.1.0',$5::jsonb)""",
        spec.id,
        KEY,
        spec.title,
        "d" * 40,
        canonical_json(definition).decode(),
    )
    inputs = {
        "audit_run_id": str(uuid4()),
        "keyword_run_id": str(uuid4()),
        "start_date": datetime.now(UTC).date().isoformat(),
        "duration": "6_months",
        "context_files": [],
        "pieces_per_batch": 2,
        "amendment_id": "",
    }
    configured = await database.create_project_workflow(
        project_id=project.id,
        workflow_id=spec.id,
        definition_commit_sha="d" * 40,
        name="Content proof",
        inputs=inputs,
        input_schema=spec.input_schema,
        schedule=None,
        request_id=uuid4(),
        created_by_clerk_user_id="user_test",
    )
    await database.project_workflow_synced(
        project_workflow_id=configured.id, temporal_schedule_id=None, next_run_at=None
    )

    async def sources(**kwargs):
        return {
            "scope": {"host": "example.com", "market": "US"},
            "sources": {"audit": {"revision": "c" * 40}},
            "rows": [{"source_id": "keyword:k1", "data": {"keyword": "useful query"}}],
        }

    monkeypatch.setattr("tin_lite.content_plan_activities.research_sources", sources)

    class Model:
        calls = 0

        async def generate(self, key, request, *, timeout_seconds=None):
            import json

            from test_content_plan import item

            from tin_lite.model_providers import ModelUsage

            self.calls += 1
            self.timeouts.append(timeout_seconds)
            data = json.loads(request.messages[0].content)
            if editorial:
                import jsonschema
                from test_content_plan_editorial import portfolio

                result = portfolio(min(30, data["capacity"]))
                typed = "TypedOpportunity" in request.output_schema["$defs"]
                for opportunity in result["opportunities"]:
                    opportunity["source_ids"] = [data["sources"][0]["source_id"]]
                    if typed:
                        opportunity["kind"] = "article"
                existing = [i for b in data["selected_batches"] for i in b["items"]]
                if existing:
                    for index, opportunity in enumerate(result["opportunities"]):
                        if index < len(existing):
                            opportunity.update(
                                {k: existing[index][k] for k in ("id", "title", "intent")}
                            )
                            opportunity["brief"] = "Revised useful brief."
                elif self.calls == 1:
                    result["opportunities"][0].update(action="update_page", page_id="p001")
                jsonschema.validate(result, request.output_schema)
                return SimpleNamespace(parsed=result, usage=ModelUsage(), request_id="model-test")
            plan = deepcopy(data["plan"])
            plan["strategy"] = "Evidence-backed small program."
            target = next(batch for batch in plan["batches"] if batch["id"] in data["editable"])
            if not target["items"]:
                target["items"] = [item()]
            else:
                target["items"][0]["brief"] = "Revised useful brief."
            return SimpleNamespace(parsed=plan, usage=ModelUsage(), request_id="model-test")

    model = Model()
    model.fetches, model.timeouts = [], []

    async def fetch(url, *, host):
        assert host == "example.com"
        model.fetches.append(url)
        return {
            "url": url,
            "html": "<h1>API product</h1><p>Supported product tasks include receiving inbound "
            "messages and sending responses through the API. Implementation documentation.</p>",
            "observed_at": datetime.now(UTC).isoformat(),
            "sha256": "a" * 64,
        }

    if editorial:
        monkeypatch.setattr("tin_lite.content_plan_activities.fetch_page", fetch)
    activities = ContentPlanActivities(
        database=database,
        storage=storage,
        settings=SimpleNamespace(luna_api_key=True, content_plan_max_cost_usd=1),
        router=model,
    )

    async def run(amendment_id=None):
        created, _ = await database.create_run(
            project_id=project.id,
            workflow_id=spec.id,
            started_by_clerk_user_id="user_test",
            project_workflow_id=configured.id,
            definition_commit_sha="d" * 40,
            input_payload={**inputs, "amendment_id": str(amendment_id)} if amendment_id else inputs,
        )
        return created

    return database, storage, project, configured, activities, model, run


async def test_model_paths_are_canonicalized_with_evidence_and_raw_receipt_preserved(
    publication_db, monkeypatch
):
    import json

    db, storage, project, configured, activities, model, create = await setup(
        publication_db, monkeypatch
    )
    generate = model.generate

    async def relative_destination(*args, **kwargs):
        result = await generate(*args, **kwargs)
        result.parsed["batches"][0]["items"][0]["destination"] = "/seo-audit-workflows"
        return result

    monkeypatch.setattr(model, "generate", relative_destination)
    run = await create()
    await activities.content_plan_execute(str(run.id))
    await activities.content_plan_execute(str(run.id))
    saved = await db.get_run(run.id)
    assert saved.status.value == "succeeded" and model.calls == 1
    model_receipt = await db.get_effect(f"content:{run.id}:model")
    assert model_receipt.result["data"]["batches"][0]["items"][0]["destination"] == (
        "/seo-audit-workflows"
    )
    evidence = json.loads(
        await storage.read_canonical_artifact(
            repo_id=project.state_repo_id,
            commit_sha=saved.canonical_commit_sha,
            path=f"reports/content-plan/{run.id}/evidence.json",
        )
    )
    assert evidence["normalized_destinations"] == [
        {
            "item_id": "topic_1",
            "from": "/seo-audit-workflows",
            "to": "https://example.com/seo-audit-workflows",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("editorial", [False, True])
async def test_initial_retry_atomic_working_plan_and_free_batch(
    publication_db, monkeypatch, editorial
):
    db, storage, project, configured, activities, model, create = await setup(
        publication_db, monkeypatch, editorial=editorial
    )
    initial = await create()
    storage.repo.lose_response = True
    with pytest.raises(PublicationPendingError):
        await activities.execute(str(initial.id))
    storage.repo.edit({"notes.md": b"Concurrent member note"})
    await activities.execute(str(initial.id))
    await activities.execute(str(initial.id))
    assert model.calls == 1
    assert len(model.fetches) == (1 if editorial else 0)
    assert storage.repo.writes == 1
    run = await db.get_run(initial.id)
    assert run.status.value == "succeeded"
    assert plan_path(configured.id) in storage.repo.trees[run.canonical_commit_sha]
    weekly = await create()
    await activities.execute(str(weekly.id))
    await activities.execute(str(weekly.id))
    facts = await activities.programs.facts(configured.id)
    assert len(facts["batches"]) == 1
    assert facts["batches"][0]["status"] == "prepared"
    assert model.calls == 1


@pytest.mark.asyncio
async def test_delayed_batch_does_not_start_after_program_end(publication_db, monkeypatch):
    db, storage, project, configured, activities, model, create = await setup(
        publication_db, monkeypatch
    )
    initial = await create()
    await activities.execute(str(initial.id))
    delayed = await create()

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=190)

    monkeypatch.setattr("tin_lite.content_plan_activities.datetime", Later)
    await activities.execute(str(delayed.id))
    assert not (await activities.programs.facts(configured.id))["batches"]
    assert (await db.get_run(delayed.id)).result_summary.startswith("Content program finished")
    assert model.calls == 1


@pytest.mark.asyncio
async def test_program_research_is_locked_before_first_activity(publication_db, monkeypatch):
    db, storage, project, configured, activities, model, create = await setup(
        publication_db, monkeypatch
    )
    await create()
    with pytest.raises(ValueError, match="pinned from the first run"):
        await db.update_project_workflow(
            project_workflow_id=configured.id,
            project_id=project.id,
            name=configured.name,
            inputs={**configured.inputs, "duration": "2_weeks"},
            schedule=None,
            expected_settings_revision=configured.settings_revision,
            clerk_user_id="user_test",
            changed_fields=["duration"],
            workflow_key=KEY,
            workflow_title="Content proof",
        )
    assert model.calls == 0


@pytest.mark.asyncio
async def test_revision_event_failure_rolls_back_hold(publication_db, monkeypatch):
    db, storage, project, configured, activities, model, create = await setup(
        publication_db, monkeypatch
    )
    initial = await create()
    await activities.execute(str(initial.id))
    original_event = activities.programs.event

    async def fail_event(*args):
        raise RuntimeError("injected event failure")

    monkeypatch.setattr(activities.programs, "event", fail_event)
    request = dict(
        project_id=project.id,
        program_id=configured.id,
        request_id=uuid4(),
        expected_revision=storage.repo.head,
        batch_ids=["week_01"],
        instruction="Improve the brief.",
        context_paths=[],
        actor="user_test",
    )
    with pytest.raises(RuntimeError, match="injected"):
        await activities.programs.begin_revision(**request)
    assert (await activities.programs.facts(configured.id))["pending_revision"] is None
    monkeypatch.setattr(activities.programs, "event", original_event)
    await activities.programs.begin_revision(**request)
    assert (await activities.programs.facts(configured.id))["pending_revision"]


@pytest.mark.asyncio
@pytest.mark.parametrize("editorial", [False, True])
async def test_amendment_hold_discard_and_protected_batch(publication_db, monkeypatch, editorial):
    db, storage, project, configured, activities, model, create = await setup(
        publication_db, monkeypatch, editorial=editorial
    )
    initial = await create()
    await activities.execute(str(initial.id))
    programs = ContentPrograms(database=db, storage=storage)
    read = await programs.read(project_id=project.id, program_id=configured.id)
    request = uuid4()
    await programs.begin_revision(
        project_id=project.id,
        program_id=configured.id,
        request_id=request,
        expected_revision=read["revision"],
        batch_ids=["week_01"],
        instruction="Improve the brief",
        context_paths=[],
        actor="user_test",
    )
    tick = await create()
    await activities.execute(str(tick.id))
    assert not (await programs.facts(configured.id))["batches"]
    preview = await create(request)
    await activities.execute(str(preview.id))
    assert model.calls == 2
    assert (await programs.read(project_id=project.id, program_id=configured.id))["plan"] == read[
        "plan"
    ]
    await programs.resolve(
        project_id=project.id,
        program_id=configured.id,
        revision_id=request,
        action="apply",
        expected_revision=storage.repo.head,
        actor="user_test",
    )
    after = await programs.read(project_id=project.id, program_id=configured.id)
    assert after["plan"]["batches"][0]["items"][0]["brief"] == "Revised useful brief."
    assert (await programs.facts(configured.id))["pending_revision"] is None
    next_tick = await create()
    await activities.execute(str(next_tick.id))
    proposed = deepcopy(after["plan"])
    proposed["batches"][0]["items"] = []
    with pytest.raises(ValueError, match="prepared"):
        await programs.save(
            project_id=project.id,
            program_id=configured.id,
            request_id=uuid4(),
            expected_revision=after["revision"],
            proposed=proposed,
            actor="user_test",
        )


@pytest.mark.asyncio
async def test_stale_preview_never_overwrites_and_can_be_discarded(publication_db, monkeypatch):
    db, storage, project, configured, activities, model, create = await setup(
        publication_db, monkeypatch
    )
    initial = await create()
    await activities.execute(str(initial.id))
    programs = activities.programs
    read = await programs.read(project_id=project.id, program_id=configured.id)
    request = uuid4()
    await programs.begin_revision(
        project_id=project.id,
        program_id=configured.id,
        request_id=request,
        expected_revision=read["revision"],
        batch_ids=["week_01"],
        instruction="Improve the brief",
        context_paths=[],
        actor="user_test",
    )
    preview = await create(request)
    await activities.execute(str(preview.id))
    with pytest.raises(StaleProjectRevisionError):
        await programs.resolve(
            project_id=project.id,
            program_id=configured.id,
            revision_id=request,
            action="apply",
            expected_revision=read["revision"],
            actor="user_test",
        )
    assert (await programs.facts(configured.id))["pending_revision"]["status"] == "pending"
    assert (await programs.read(project_id=project.id, program_id=configured.id))["plan"] == read[
        "plan"
    ]
    for _ in range(2):
        assert (
            await programs.resolve(
                project_id=project.id,
                program_id=configured.id,
                revision_id=request,
                action="discard",
                actor="user_test",
            )
        )["status"] == "discarded"


@pytest.mark.asyncio
async def test_uncertain_apply_cannot_be_mistaken_for_discardable_stale_edit(
    publication_db, monkeypatch
):
    db, storage, project, configured, activities, model, create = await setup(
        publication_db, monkeypatch
    )
    await activities.execute(str((await create()).id))
    programs, request = activities.programs, uuid4()
    await programs.begin_revision(
        project_id=project.id,
        program_id=configured.id,
        request_id=request,
        expected_revision=storage.repo.head,
        batch_ids=["week_01"],
        instruction="Improve the brief",
        context_paths=[],
        actor="user_test",
    )
    await activities.execute(str((await create(request)).id))
    commit = storage.commit_project_changes

    async def lost_ack(**kwargs):
        await commit(**kwargs)
        storage.repo.edit({"notes.md": b"Another member's later edit"})
        raise ConnectionError("Synthetic lost reply")

    monkeypatch.setattr(storage, "commit_project_changes", lost_ack)
    args = dict(
        project_id=project.id,
        program_id=configured.id,
        revision_id=request,
        actor="user_test",
        expected_revision=storage.repo.head,
    )
    with pytest.raises(ConnectionError):
        await programs.resolve(**args, action="apply")
    monkeypatch.setattr(storage, "commit_project_changes", commit)
    with pytest.raises(RuntimeError, match="earlier Apply remains unconfirmed"):
        await programs.resolve(**args, action="apply")
    assert (await programs.facts(configured.id))["pending_revision"]["status"] == "applying"
    with pytest.raises(ValueError, match="being reconciled"):
        await programs.resolve(**args, action="discard")


@pytest.mark.asyncio
async def test_saved_edit_replays_after_batch_starts_and_invalid_raw_edits_block(
    publication_db, monkeypatch
):
    db, storage, project, configured, activities, model, create = await setup(
        publication_db, monkeypatch
    )
    await activities.execute(str((await create()).id))
    read = await activities.programs.read(project_id=project.id, program_id=configured.id)
    proposed = deepcopy(read["plan"])
    proposed["batches"][0]["items"][0]["brief"] = "A member's exact new brief."
    args = dict(
        project_id=project.id,
        program_id=configured.id,
        request_id=uuid4(),
        expected_revision=read["revision"],
        proposed=proposed,
        actor="user_test",
    )
    result = await activities.programs.save(**args)
    batch = await create()
    await activities.execute(str(batch.id))
    replay = await activities.programs.save(**args)
    assert replay.replayed and replay.revision == result.revision
    storage.repo.edit({plan_path(configured.id): b"{not json}"})
    with pytest.raises(ValueError):
        await activities.execute(str((await create()).id))
    facts = await activities.programs.facts(configured.id)
    assert len(facts["batches"]) == 1 and model.calls == 1
    storage.repo.edit({plan_path(configured.id): None})
    with pytest.raises(ValueError, match="missing"):
        await activities.programs.read(project_id=project.id, program_id=configured.id)


@pytest.mark.asyncio
async def test_known_initial_failure_can_start_again_but_unknown_model_is_not_rebought(
    publication_db, monkeypatch
):
    db, storage, project, configured, activities, model, create = await setup(
        publication_db, monkeypatch
    )
    activities.settings.content_plan_max_cost_usd = 0
    initial = await create()
    with pytest.raises(ValueError, match="spending"):
        await activities.execute(str(initial.id))
    await activities.content_plan_failure(str(initial.id))
    activities.settings.content_plan_max_cost_usd = 1
    second = await create()
    await activities.execute(str(second.id))
    assert model.calls == 1 and (await db.get_run(second.id)).status.value == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("editorial", [False, True])
async def test_ambiguous_model_retry_does_not_buy_another_call(
    publication_db, monkeypatch, editorial
):
    db, storage, project, configured, activities, model, create = await setup(
        publication_db, monkeypatch, editorial=editorial
    )
    calls = 0

    async def unknown(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise ConnectionError("Synthetic lost response")

    monkeypatch.setattr(model, "generate", unknown)
    run = await create()
    for _ in range(2):
        with pytest.raises(ValueError, match="unconfirmed"):
            await activities.execute(str(run.id))
    assert calls == 1 and storage.repo.writes == 0
    assert len(model.fetches) == (1 if editorial else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("editorial", [False, True])
async def test_plan_waits_for_its_full_output_instead_of_the_client_default(
    publication_db, monkeypatch, editorial
):
    from tin_lite.content_plan_activities import MODEL_TIMEOUT_SECONDS

    db, storage, project, configured, activities, model, create = await setup(
        publication_db, monkeypatch, editorial=editorial
    )
    run = await create()
    await activities.execute(str(run.id))
    # 16,000 output tokens at about 55 tokens a second needs close to five minutes; the
    # provider's 90-second default cut production plans off after about 100 seconds.
    assert model.timeouts == [MODEL_TIMEOUT_SECONDS]
    assert MODEL_TIMEOUT_SECONDS >= legacy.POLICY["max_output_tokens"] / 55 > 90


async def test_editorial_page_failure_is_evidence_not_a_guessed_update(publication_db, monkeypatch):
    import json

    db, storage, project, configured, activities, model, create = await setup(
        publication_db, monkeypatch, editorial=True
    )

    async def unavailable(*args, **kwargs):
        raise OSError("Untrusted network error containing private details")

    monkeypatch.setattr("tin_lite.content_plan_activities.fetch_page", unavailable)
    # A valid new-page proposal is still possible when current page inspection is unavailable.
    model.calls = 1
    run = await create()
    await activities.execute(str(run.id))
    saved = await db.get_run(run.id)
    evidence = json.loads(
        await storage.read_canonical_artifact(
            repo_id=project.state_repo_id,
            commit_sha=saved.canonical_commit_sha,
            path=legacy.paths(str(run.id))["evidence.json"],
        )
    )
    assert evidence["live_pages_checked"] is False
    assert evidence["page_inventory"]["pages"][0]["status"] == "unavailable"
    assert "private details" not in json.dumps(evidence)
    assert evidence["editorial"]["planned_items"] == 30


async def test_editorial_validation_failure_is_receipted_without_repair_purchase(
    publication_db, monkeypatch
):
    db, storage, project, configured, activities, model, create = await setup(
        publication_db, monkeypatch, editorial=True
    )
    generate = model.generate

    async def mismatched(*args, **kwargs):
        result = await generate(*args, **kwargs)
        result.parsed["opportunities"][0]["page_id"] = "p_missing"
        return result

    monkeypatch.setattr(model, "generate", mismatched)
    run = await create()
    for _ in range(2):
        with pytest.raises(ValueError, match="inspected"):
            await activities.execute(str(run.id))
    assert model.calls == 1 and len(model.fetches) == 1 and storage.repo.writes == 0
    assert (await db.get_effect(f"content:{run.id}:model")).status == "completed"
