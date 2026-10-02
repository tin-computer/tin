"""Real disposable-DB X guide proposal, approval, receipt and publication path."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from temporalio.exceptions import ApplicationError
from test_private_workflows import ACTOR
from test_private_workflows import fixture as project_fixture
from test_procedure_publication import publication_db as publication_db

from tin_lite import x_style
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.code_storage import CodeStorage
from tin_lite.integrations import IntegrationAuthorizationError
from tin_lite.model_providers import ModelResult, ModelUsage, ProviderName
from tin_lite.output_resolution import OutputResolutionService
from tin_lite.project_files import ProjectFileService
from tin_lite.publication import read_run_output
from tin_lite.run_service import start_workflow_run
from tin_lite.workflow_inputs import WorkflowInputError
from tin_lite.x_style_activities import XStyleActivities

SAMPLES = (
    "I built a small workflow that returns a draft as a project file.\n\n"
    "A useful demo should show the exact edit and result, not just a launch claim."
)
RESULT = {
    "summary": "Direct technical notes with a clear example.",
    "limitations": "These two samples show a narrow slice of the account's writing.",
    "voice": [{"rule": "Use plain technical language.", "sources": ["s1"]}],
    "structure": [{"rule": "Open with the concrete behavior.", "sources": ["s1"]}],
    "vocabulary": [{"rule": "Name the actual file or edit.", "sources": ["s2"]}],
    "avoid": [{"rule": "Avoid broad launch claims.", "sources": ["s2"]}],
    "demonstration": "The file now shows the exact next step. You can check it before acting.",
}


async def fixture(db, *, source_path=False, version=2):
    f = await project_fixture(db)
    await db.upsert_workflow_system(
        system_id="organic-traffic", name="Organic traffic", display_order=1
    )
    builtin = next(item for item in BUILTIN_WORKFLOWS if item.key == x_style.KEY)
    definition = builtin.definition
    if version == 1:
        # A run pinned to the 1.0.0 definition.
        policy, instructions = x_style.CONTRACTS[1]
        definition = {**definition, "x_style_policy": policy, "x_style_instructions": instructions}
    revision = "d" * 40
    await db.upsert_registry_workflow(
        workflow_id=builtin.id,
        key=x_style.KEY,
        title=builtin.title,
        description=builtin.description,
        executor=x_style.KEY,
        definition_repo_id="registry/workflows",
        definition_path=builtin.definition_path,
        current_commit_sha=revision,
        version_label=builtin.version_label,
        definition=definition,
    )
    f.workflow = await db.get_workflow(builtin.id)
    f.settings.luna_api_key = "test-native-key"
    if source_path:
        f.storage.repo.edit({"style/sources/x-posts.md": SAMPLES.encode()})
    original_read = f.storage.read_canonical_artifact

    async def read(**kwargs):
        if kwargs["repo_id"] == "registry/workflows":
            assert kwargs["commit_sha"] == revision
            assert kwargs["path"] == builtin.definition_path
            return json.dumps(definition).encode()
        return await original_read(**kwargs)

    async def stage(**kwargs):
        # Exercise the real executor/path validation; fake only the remote commit.
        # An untyped staging mock missed X capture falling back to style.capture.
        files = {}

        async def send():
            head = f.storage.repo.head
            saved = f.storage.repo.edit(files)
            f.storage.repo.head = head
            return {"commit_sha": saved}

        builder = SimpleNamespace(send=send)

        def add_file(path, content):
            files[path] = content
            return builder

        builder.add_file = add_file
        stager = object.__new__(CodeStorage)
        stager.procedure_checkpoint_revision = AsyncMock(return_value=None)
        stager.get_repo = AsyncMock(
            return_value=SimpleNamespace(create_commit=lambda **options: builder)
        )
        return await stager.stage_native_output(**kwargs)

    f.storage.read_canonical_artifact = read
    f.storage.stage_native_output = AsyncMock(side_effect=stage)
    f.runtime.project_files = ProjectFileService(database=db, storage=f.storage)
    f.router = SimpleNamespace(
        generate=AsyncMock(
            return_value=ModelResult(
                provider=ProviderName.OPENAI,
                model="gpt-6-sol",
                text="",
                parsed=deepcopy(RESULT),
                request_id="x-style-request",
                usage=ModelUsage(input_tokens=100, output_tokens=50),
            )
        )
    )
    # X isn't connected here: Auto uses the supplied writing alone.
    disconnected = AsyncMock(
        side_effect=IntegrationAuthorizationError("Connect or reconnect X in this project")
    )
    f.activities = XStyleActivities(
        database=db,
        storage=f.storage,
        router=f.router,
        x_connection=SimpleNamespace(connection=disconnected),
    )
    return f


async def start(f, *, source_path=False):
    payload = {
        "account_id": "12345",
        "preferences": "Keep product claims precise.",
        "direction": "Learn my short technical notes.",
    }
    payload["source_path" if source_path else "supplied_samples"] = (
        "style/sources/x-posts.md" if source_path else SAMPLES
    )
    return await start_workflow_run(
        runtime=f.runtime,
        settings=f.settings,
        workflow=f.workflow,
        project_id=f.project.id,
        started_by_clerk_user_id=ACTOR,
        start_idempotency_key=str(uuid4()),
        input_payload=payload,
    )


async def test_x_style_adopts_edited_proposal_and_preserves_receipts(publication_db):
    f = await fixture(publication_db, source_path=True)
    old_guide = (
        b"# X writing style\n\nX account ID: 12345\n\n## Explicit preferences\n\nNo hashtags.\n"
    )
    f.storage.repo.edit({x_style.GUIDE_PATH: old_guide})
    run = await start(f, source_path=True)
    await f.activities.prepare(str(run.id))
    await f.activities.prepare(str(run.id))
    f.storage.repo.edit({"style/sources/x-posts.md": b"CHANGED AFTER PIN"})
    await f.activities.extract(str(run.id))
    await f.activities.extract(str(run.id))
    receipt = await f.db.get_effect(f"{run.id}:x_style_model")
    assert receipt.status == "completed"
    assert "I built a small workflow" not in json.dumps(receipt.result)
    assert "CHANGED AFTER PIN" not in f.router.generate.await_args.args[1].messages[0].content
    assert f.router.generate.await_count == 1
    assert "No hashtags." in receipt.result["guide"]
    assert await f.activities.propose(str(run.id))
    assert await f.activities.propose(str(run.id))
    waiting = await f.db.get_run(run.id)
    assert waiting.status.value == "needs_input" and waiting.review_required
    assert f.storage.repo.trees[f.storage.repo.head][x_style.GUIDE_PATH][1] == old_guide
    proposal = waiting.artifact_path
    edited = (
        b"# X writing style\n\nX account ID: 12345\n\n"
        b"## Explicit preferences\n\nNo hashtags. Use dry humor.\n"
    )
    f.storage.repo.edit({proposal: edited})
    await f.activities.record_approval(str(run.id))
    await f.activities.record_approval(str(run.id))
    await f.activities.publish(str(run.id))
    await f.activities.publish(str(run.id))
    done = await f.db.get_run(run.id)
    assert done.status.value == "succeeded" and done.artifact_path == x_style.GUIDE_PATH
    assert done.retained_output is None and done.review_decision == "approved"
    output = await read_run_output(storage=f.storage, run=done, repo_id=f.project.state_repo_id)
    assert output.content == edited
    assert f.storage.repo.trees[f.storage.repo.head][x_style.GUIDE_PATH][1] == edited
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM activity_events WHERE run_id=$1 AND event_type='x_style_ready'",
            run.id,
        )
        == 1
    )


@pytest.mark.parametrize(
    ("protected", "account", "message"),
    [(True, "12345", "public X account"), (False, "67890", "differs from")],
)
async def test_connected_source_checks_account_before_admission_with_preferences(
    publication_db, protected, account, message
):
    f = await fixture(publication_db)
    connection = AsyncMock(
        return_value=SimpleNamespace(
            configuration={"protected": protected}, external_account_id=account
        )
    )
    f.runtime.integrations = SimpleNamespace(x=SimpleNamespace(connection=connection))
    with pytest.raises(WorkflowInputError, match=message):
        await start_workflow_run(
            runtime=f.runtime,
            settings=f.settings,
            workflow=f.workflow,
            project_id=f.project.id,
            started_by_clerk_user_id=ACTOR,
            start_idempotency_key=str(uuid4()),
            input_payload={
                "sample_source": "connected",
                "preferences": "Keep technical details.",
                "account_id": "12345",
            },
        )
    connection.assert_awaited_once_with(f.project.id, capability="x.posts.read")
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 0


async def test_x_style_conflict_preserves_later_user_edit(publication_db):
    f = await fixture(publication_db)
    run = await start(f)
    await f.activities.prepare(str(run.id))
    await f.activities.extract(str(run.id))
    assert await f.activities.propose(str(run.id))
    await f.activities.record_approval(str(run.id))
    later = b"# X writing style\n\nX account ID: 12345\n\nLater human edit.\n"
    f.storage.repo.edit({x_style.GUIDE_PATH: later})
    with pytest.raises(ApplicationError, match="guide changed"):
        await f.activities.publish(str(run.id))
    await f.activities.failure(str(run.id))
    failed = await f.db.get_run(run.id)
    assert failed.retained_output["reason"] == "output_conflict"
    comparison = await OutputResolutionService(database=f.db, storage=f.storage).compare(
        run_id=run.id
    )
    assert comparison["current"]["content"] == later.decode()
    assert "X account ID: 12345" in comparison["saved"]["content"]
    assert f.storage.repo.trees[f.storage.repo.head][x_style.GUIDE_PATH][1] == later


async def test_x_style_uncertain_model_not_rebought(publication_db):
    f = await fixture(publication_db)
    run = await start(f)
    await f.activities.prepare(str(run.id))
    f.router.generate.side_effect = ConnectionError("private provider URL")
    for _ in range(2):
        with pytest.raises(ApplicationError):
            await f.activities.extract(str(run.id))
    assert f.router.generate.await_count == 1
    assert f.storage.repo.writes == 0


@pytest.mark.parametrize("version", [1, 2])
async def test_a_run_keeps_the_sampling_of_the_version_it_was_pinned_to(publication_db, version):
    f = await fixture(publication_db, version=version)
    run = await start(f)
    await f.activities.prepare(str(run.id))
    context = await f.db.get_effect(f"{run.id}:x_style_context")
    assert context.result["policy_version"] == version
    await f.activities.extract(str(run.id))
    request = f.router.generate.await_args.args[1]
    assert request.system == x_style.CONTRACTS[version][1]
    samples = json.loads(request.messages[0].content)["samples"]
    assert len(samples) == 2
    assert all(("kind" in item) == (version == 2) for item in samples)
