import json
from copy import deepcopy
from datetime import timedelta
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
from zipfile import ZipFile

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError
from temporalio import workflow as temporal_workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from test_private_workflows import ACTOR, app, mcp, structured
from test_private_workflows import fixture as project_fixture
from test_procedure_publication import publication_db as publication_db
from test_project_codex_execution import temporal_env as temporal_env

from tin_lite.billing_contracts import test_terms as billing_test_terms
from tin_lite.catalog import BUILTIN_WORKFLOWS
from tin_lite.code_storage import CodeStorage
from tin_lite.model_providers import ModelResult, ModelUsage, ProviderName
from tin_lite.output_resolution import OutputResolutionRequest, OutputResolutionService
from tin_lite.project_files import ProjectFileService
from tin_lite.publication import PublicationPendingError, read_run_output
from tin_lite.run_service import start_workflow_run
from tin_lite.style_capture import (
    KEY,
    SourcePacket,
    explicit_preferences,
    packet_markdown,
    parse_packet,
    render_guide,
)
from tin_lite.style_capture_activities import StyleCaptureActivities
from tin_lite.style_samples import extract_sample
from tin_lite.workflow_inputs import WorkflowInputError
from tin_lite.writing_style import STYLE_PATH, style_capture_preparation

SOURCE = "style/sources/test.md"
PACKET = SourcePacket.model_validate(
    {
        "purpose": "Clear founder articles",
        "preferences": "Use concrete examples.",
        "samples": [
            {
                "id": "s1",
                "label": "Editing direction",
                "kind": "correction",
                "origin": "User-selected conversation",
                "text": "Keep it graceful and simple.",
            }
        ],
    }
)
RESULT = {
    "summary": "Direct and concrete.",
    "limitations": "Only conversational evidence, not essays.",
    "voice": [{"rule": "Explain the practical choice.", "sources": ["s1"]}],
    "structure": [{"rule": "Start with the question.", "sources": ["s1"]}],
    "vocabulary": [{"rule": "Use ordinary words.", "sources": ["s1"]}],
    "avoid": [{"rule": "Avoid unnecessary ceremony.", "sources": ["s1"]}],
    "demonstration": "What needs to happen next? Start with one small change and check it works.",
}


def test_source_roundtrip_and_honest_basis():
    assert parse_packet(packet_markdown(PACKET).encode()) == PACKET
    for kind, basis in [
        ("authored", "sample-based"),
        ("note", "provisional"),
        ("conversation", "provisional"),
        ("reference", "provisional"),
    ]:
        packet = PACKET.model_copy(deep=True)
        packet.samples[0].kind = kind
        guide = render_guide(RESULT, packet, source_path=SOURCE, revision="a" * 40).decode()
        assert basis in guide and "Use concrete examples." in guide
    packet = SourcePacket(purpose="Articles", preferences="Keep it brief.")
    result = deepcopy(RESULT)
    for name in ("voice", "structure", "vocabulary", "avoid"):
        result[name][0]["sources"] = []
    assert b"preferences-only" in render_guide(
        result, packet, source_path=SOURCE, revision="a" * 40
    )


def test_empty_duplicate_and_assistant_samples_rejected():
    with pytest.raises(ValidationError):
        SourcePacket(purpose="Articles")
    for changes in (
        {"samples": [PACKET.samples[0].model_dump()] * 2},
        {"samples": [{**PACKET.samples[0].model_dump(), "kind": "assistant"}]},
    ):
        with pytest.raises(ValidationError):
            SourcePacket.model_validate({**PACKET.model_dump(), **changes})
    with pytest.raises(ValueError):
        parse_packet(b"x" * 100001)
    with pytest.raises(ValueError):
        parse_packet(b"not a source packet")


def test_no_hallucinated_source_and_preserve_preferences():
    invalid = deepcopy(RESULT)
    invalid["voice"][0]["sources"] = ["invented"]
    with pytest.raises(ValueError, match="unavailable sample"):
        render_guide(invalid, PACKET, source_path=SOURCE, revision="a" * 40)
    guide = render_guide(
        RESULT,
        PACKET,
        source_path=SOURCE,
        revision="a" * 40,
        existing_preferences="Never invent personal experience.",
    ).decode()
    assert "Never invent personal experience." in explicit_preferences(guide)
    assert "Use concrete examples." in explicit_preferences(guide)


def docx(body):
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr(
            "word/document.xml",
            f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>',
        )
    return buffer.getvalue()


def test_document_conversion_is_bounded_and_text_only():
    assert extract_sample("sample.md", b"# Hello")["text"] == "# Hello"
    assert extract_sample("sample.txt", b"\xef\xbb\xbfHello")["text"] == "Hello"
    result = extract_sample(
        "sample.docx",
        docx(
            "<w:p><w:r><w:t>Hello</w:t></w:r><w:r><w:rPr><w:vanish/></w:rPr>"
            "<w:t>Secret comment</w:t></w:r></w:p>"
        ),
    )
    assert result["text"] == "Hello" and result["warnings"]
    for filename, content in [
        ("x.pdf", b"text"),
        ("x.docx", b"bad"),
        ("x.txt", b"\xff"),
        ("x.txt", b"x" * 100001),
        ("x.txt", b" "),
        ("x.docx", docx("<w:ins><w:r><w:t>Inserted</w:t></w:r></w:ins>")),
    ]:
        with pytest.raises(ValueError):
            extract_sample(filename, content)


async def capture_fixture(db, *, reviewed=True):
    f = await project_fixture(db)
    await db.upsert_workflow_system(
        system_id="organic-traffic", name="Organic traffic", display_order=1
    )
    builtin = next(w for w in BUILTIN_WORKFLOWS if w.key == KEY)
    definition = builtin.definition
    if not reviewed:  # A run pinned to the definition from before review.
        definition = {k: v for k, v in definition.items() if k != "human_review"}
    revision = "d" * 40
    await db.upsert_registry_workflow(
        workflow_id=builtin.id,
        key=KEY,
        title=builtin.title,
        description=builtin.description,
        executor=KEY,
        definition_repo_id="registry/workflows",
        definition_path=builtin.definition_path,
        current_commit_sha=revision,
        version_label=builtin.version_label,
        definition=definition,
    )
    f.workflow = await db.get_workflow(builtin.id)
    f.settings.luna_api_key = "test-native-key"
    f.storage.repo.edit({SOURCE: packet_markdown(PACKET).encode()})
    original_read = f.storage.read_canonical_artifact

    async def read(**kw):
        if kw["repo_id"] == "registry/workflows":
            assert kw["commit_sha"] == revision and kw["path"] == builtin.definition_path
            return json.dumps(definition).encode()
        return await original_read(**kw)

    async def stage(**kw):
        head = f.storage.repo.head
        saved = f.storage.repo.edit({kw["path"]: kw["content"]})
        f.storage.repo.head = head
        return saved

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
                request_id="style-request",
                usage=ModelUsage(input_tokens=100, output_tokens=50),
            )
        )
    )
    f.activities = StyleCaptureActivities(database=db, storage=f.storage, router=f.router)
    return f


async def approve(f, run):
    assert await f.activities.propose(str(run.id))
    await f.activities.record_approval(str(run.id))


async def start(f, *, key=None):
    return await start_workflow_run(
        runtime=f.runtime,
        settings=f.settings,
        workflow=f.workflow,
        project_id=f.project.id,
        started_by_clerk_user_id=ACTOR,
        start_idempotency_key=key or str(uuid4()),
        input_payload={"source_path": SOURCE},
    )


async def test_native_capture_pins_inputs_retries_and_projects(publication_db):
    f = await capture_fixture(publication_db)
    f.storage.repo.edit({STYLE_PATH: b"My current guide\n"})
    key = str(uuid4())
    run = await start(f, key=key)
    assert (await start(f, key=key)).id == run.id
    for _ in range(2):
        await f.activities.prepare(str(run.id))
    f.storage.repo.edit({SOURCE: b"changed after preparation"})
    for _ in range(2):
        await f.activities.extract(str(run.id))
    for _ in range(2):
        assert await f.activities.propose(str(run.id))
    waiting = await f.db.get_run(run.id)
    assert waiting.status.value == "needs_input" and waiting.review_required
    proposal = waiting.artifact_path
    assert proposal.startswith("style/proposals/") and proposal.endswith(
        f"-writing-style-{str(run.id)[:8]}.md"
    )
    assert waiting.artifact_title == "Proposed writing style guide"
    head = f.storage.repo.trees[f.storage.repo.head]
    assert head[STYLE_PATH][1] == b"My current guide\n" and b"provisional" in head[proposal][1]
    decisions = await f.db.list_pending_decisions(project_id=f.project.id)
    assert [d["run_id"] for d in decisions] == [run.id]
    for _ in range(2):
        await f.activities.record_approval(str(run.id))
    for _ in range(2):
        await f.activities.publish(str(run.id))
    done = await f.db.get_run(run.id)
    assert done.status.value == "succeeded" and done.artifact_path == STYLE_PATH
    assert done.retained_output is None and done.review_decision == "approved"
    assert f.router.generate.await_count == 1 and f.storage.stage_native_output.await_count == 1
    result = await read_run_output(storage=f.storage, run=done, repo_id=f.project.state_repo_id)
    assert b"provisional" in result.content and done.expected_head_sha.encode() in result.content
    assert f.storage.repo.writes == 2  # the proposal and the saved guide
    assert (
        await f.db.pool.fetchval(
            "SELECT count(*) FROM activity_events "
            "WHERE run_id=$1 AND event_type='style_capture_ready'",
            run.id,
        )
        == 1
    )


async def test_guide_waits_for_approval_and_saves_the_approved_edit(publication_db):
    f = await capture_fixture(publication_db)
    run = await start(f)
    await f.activities.prepare(str(run.id))
    await f.activities.extract(str(run.id))
    assert await f.activities.propose(str(run.id))
    with pytest.raises(ApplicationError, match="waits for your approval"):
        await f.activities.publish(str(run.id))
    assert STYLE_PATH not in f.storage.repo.trees[f.storage.repo.head]
    proposal = (await f.db.get_run(run.id)).artifact_path
    f.storage.repo.edit({proposal: b"# Writing style\n\nMy corrected guide.\n"})
    await f.activities.record_approval(str(run.id))
    await f.activities.publish(str(run.id))
    head = f.storage.repo.trees[f.storage.repo.head]
    assert head[STYLE_PATH][1] == b"# Writing style\n\nMy corrected guide.\n"


async def test_removed_proposal_leaves_the_current_guide(publication_db):
    f = await capture_fixture(publication_db)
    f.storage.repo.edit({STYLE_PATH: b"My current guide\n"})
    run = await start(f)
    await f.activities.prepare(str(run.id))
    await f.activities.extract(str(run.id))
    assert await f.activities.propose(str(run.id))
    proposal = (await f.db.get_run(run.id)).artifact_path
    f.storage.repo.edit({proposal: b""})
    with pytest.raises(ApplicationError, match="removed or is too large"):
        await f.activities.record_approval(str(run.id))
    assert f.storage.repo.trees[f.storage.repo.head][STYLE_PATH][1] == b"My current guide\n"


async def test_runs_pinned_before_review_save_the_guide_directly(publication_db):
    f = await capture_fixture(publication_db, reviewed=False)
    run = await start(f)
    await f.activities.prepare(str(run.id))
    await f.activities.extract(str(run.id))
    assert not await f.activities.propose(str(run.id))
    await f.activities.publish(str(run.id))
    done = await f.db.get_run(run.id)
    assert done.status.value == "succeeded" and done.artifact_path == STYLE_PATH
    assert not done.review_required and f.storage.repo.writes == 1


async def test_capture_conflict_reuses_the_existing_comparison(publication_db):
    f = await capture_fixture(publication_db)
    run = await start(f)
    await f.activities.prepare(str(run.id))
    await f.activities.extract(str(run.id))
    await approve(f, run)
    f.storage.repo.edit({STYLE_PATH: b"My concurrently edited guide\n"})
    with pytest.raises(ApplicationError, match="guide changed"):
        await f.activities.publish(str(run.id))
    await f.activities.failure(str(run.id))
    done = await f.db.get_run(run.id)
    assert done.retained_output["reason"] == "output_conflict"
    service = OutputResolutionService(database=f.db, storage=f.storage)
    comparison = await service.compare(run_id=run.id)
    assert comparison["current"]["content"] == "My concurrently edited guide\n"
    assert "provisional" in comparison["saved"]["content"]
    response = await service.resolve(
        run_id=run.id,
        actor_clerk_user_id=ACTOR,
        client_id=None,
        request=OutputResolutionRequest(
            request_id=uuid4(),
            action="use_saved",
            expected_revision=comparison["current"]["revision"],
            saved_revision=comparison["saved"]["revision"],
        ),
    )
    assert response["state"] == "applied"
    assert f.router.generate.await_count == 1


async def test_lost_publish_response_does_not_overwrite_later_edit(publication_db):
    f = await capture_fixture(publication_db)
    run = await start(f)
    await f.activities.prepare(str(run.id))
    await f.activities.extract(str(run.id))
    await approve(f, run)
    f.storage.repo.lose_response = True
    with pytest.raises(PublicationPendingError):
        await f.activities.publish(str(run.id))
    original = f.storage.repo.head
    f.storage.repo.edit({STYLE_PATH: b"Later user correction"})
    await f.activities.publish(str(run.id))
    assert (await f.db.get_run(run.id)).canonical_commit_sha == original
    assert f.storage.repo.trees[f.storage.repo.head][STYLE_PATH][1] == b"Later user correction"
    assert f.storage.repo.writes == 2  # the proposal and the saved guide


async def test_no_model_repurchase_or_invalid_guide_replacement(publication_db):
    f = await capture_fixture(publication_db)
    run = await start(f)
    await f.activities.prepare(str(run.id))
    f.router.generate.side_effect = ConnectionError("private provider URL")
    for _ in range(2):
        with pytest.raises(ApplicationError):
            await f.activities.extract(str(run.id))
    assert f.router.generate.await_count == 1 and f.storage.repo.writes == 0


async def test_http_mcp_preflight_and_upload_authorization(publication_db, monkeypatch):
    f = await capture_fixture(publication_db)
    server = mcp(f, monkeypatch)
    contract = structured(
        await server.call_tool(
            "get_workflow",
            {
                "project_id": str(f.project.id),
                "workflow_key": KEY,
            },
        )
    )
    assert contract["system"]["id"] == "organic-traffic" and contract["runtime_available"]
    assert contract["preparation"] == style_capture_preparation(f.project.id, include_guide=True)
    f.storage.repo.edit({SOURCE: b"not useful"})
    with pytest.raises(WorkflowInputError):
        await start(f)
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 0
    for actor, status in [(ACTOR, 200), ("user_outsider", 404)]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app(f, actor)), base_url="https://tin.test"
        ) as client:
            response = await client.post(
                f"/api/projects/{f.project.id}/writing-style/preview?filename=sample.txt",
                content=b"My writing",
            )
            assert response.status_code == status
            if status == 200:
                assert response.json()["text"] == "My writing"
                assert response.headers["cache-control"] == "no-store"


async def test_discovery_and_incomplete_starts_lead_to_the_conversation(
    publication_db, monkeypatch
):
    f = await capture_fixture(publication_db)
    server = mcp(f, monkeypatch)
    catalog = structured(
        await server.call_tool("list_workflows", {"project_id": str(f.project.id)})
    )
    rows = catalog["result"] if isinstance(catalog, dict) else catalog
    capture = next(row for row in rows if row["key"] == KEY)
    prep = capture["preparation"]
    guide = structured(
        await server.call_tool(prep["next_tool"]["name"], prep["next_tool"]["arguments"])
    )
    assert guide["caller_instruction"] == prep["instruction"]
    # Existing clients can use their already-known detail tool, even with a cached tool list.
    detail = structured(
        await server.call_tool(
            "get_workflow", {"project_id": str(f.project.id), "workflow_key": KEY}
        )
    )
    assert detail["preparation"]["guide"] == guide
    assert "guide" not in prep  # Keep catalog discovery lightweight.
    for tool in ("start_workflow", "create_project_workflow"):
        args = {
            "project_id": str(f.project.id),
            "workflow_id": KEY,
            "inputs": {},
            "request_id": str(uuid4()),
        }
        if tool == "create_project_workflow":
            args["name"] = "My writing style"
        with pytest.raises(ToolError) as caught:
            await server.call_tool(tool, args)
        diagnostic = json.loads(str(caught.value).split(": ", 1)[1])
        assert diagnostic["code"] == "style_sources_required"
        assert diagnostic["next_tool"] == prep["next_tool"]
        assert "id" not in diagnostic
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 0
    assert await f.db.pool.fetchval("SELECT count(*) FROM project_workflows") == 0
    f.runtime.temporal.start_workflow.assert_not_called()
    f.router.generate.assert_not_called()


async def test_mcp_capture_start_is_ordinary_and_idempotent(publication_db, monkeypatch):
    f = await capture_fixture(publication_db)
    server = mcp(f, monkeypatch)
    args = {
        "project_id": str(f.project.id),
        "workflow_id": KEY,
        "inputs": {"source_path": SOURCE},
        "request_id": str(uuid4()),
    }
    first = structured(await server.call_tool("start_workflow", args))
    f.storage.repo.edit({SOURCE: b"changed after admission"})
    replay = structured(await server.call_tool("start_workflow", args))
    assert first["id"] == replay["id"]
    assert await f.db.pool.fetchval("SELECT count(*) FROM workflow_runs") == 1
    assert billing_test_terms(f.workflow.definition)["kind"] == "native_model"


async def test_invalid_extraction_preserves_current_guide(publication_db):
    f = await capture_fixture(publication_db)
    f.storage.repo.edit({STYLE_PATH: b"My existing guide"})
    run = await start(f)
    await f.activities.prepare(str(run.id))
    f.router.generate.return_value.parsed["voice"][0]["sources"] = ["invented"]
    await f.activities.extract(str(run.id))
    for _ in range(2):
        with pytest.raises(ApplicationError, match="invalid"):
            await f.activities.propose(str(run.id))
    assert f.router.generate.await_count == 1
    f.storage.stage_native_output.assert_not_awaited()
    assert (
        await f.storage.read_output_destination(
            repo_id=f.project.state_repo_id, revision=f.storage.repo.head, path=STYLE_PATH
        )
    )[1] == b"My existing guide"


async def test_native_checkpoint_reconciles_lost_creation_response():
    storage = object.__new__(CodeStorage)
    storage.procedure_checkpoint_revision = AsyncMock(side_effect=[None, "b" * 40])
    storage.read_procedure_checkpoint = AsyncMock(return_value=b"Guide")
    builder = SimpleNamespace(send=AsyncMock(side_effect=ConnectionError("lost reply")))
    builder.add_file = lambda *args: builder
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return builder

    storage.get_repo = AsyncMock(return_value=SimpleNamespace(create_commit=create))
    args = dict(
        repo_id="project",
        branch="main",
        run_id=str(uuid4()),
        generation=0,
        path=STYLE_PATH,
        content=b"Guide",
    )
    with pytest.raises(PublicationPendingError):
        await storage.stage_native_output(**args)
    assert await storage.stage_native_output(**args) == "b" * 40
    assert len(calls) == 1 and calls[0]["ephemeral"] is True


async def test_capture_waits_in_temporal_for_approval_and_replays(publication_db, temporal_env):
    import asyncio

    from temporalio.worker import Replayer, Worker

    from tin_lite.workflows import StyleCaptureWorkflow

    f = await capture_fixture(publication_db)
    a = f.activities
    activities = [a.prepare, a.extract, a.propose, a.record_approval, a.publish, a.failure]
    async with Worker(
        temporal_env.client,
        task_queue="style-review",
        workflows=[StyleCaptureWorkflow],
        activities=activities,
    ):
        run = await start(f)
        handle = await temporal_env.client.start_workflow(
            StyleCaptureWorkflow.run, str(run.id), id=f"style-{run.id}", task_queue="style-review"
        )
        for _ in range(200):
            if (await f.db.get_run(run.id)).status.value == "needs_input":
                break
            await asyncio.sleep(0.05)
        assert STYLE_PATH not in f.storage.repo.trees[f.storage.repo.head]
        await handle.signal("approve")
        await asyncio.wait_for(handle.result(), 30)
        history = await handle.fetch_history()
    await Replayer(workflows=[StyleCaptureWorkflow]).replay_workflow(history)
    done = await f.db.get_run(run.id)
    assert done.status.value == "succeeded" and done.artifact_path == STYLE_PATH
    assert str(run.id) in history.to_json() and "provisional" not in history.to_json()


async def test_capture_started_before_review_replays_unchanged(publication_db, temporal_env):
    import asyncio

    from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker

    from tin_lite.workflows import StyleCaptureWorkflow

    legacy = await capture_fixture(publication_db, reviewed=False)
    b = legacy.activities
    async with Worker(
        temporal_env.client,
        task_queue="style-legacy",
        workflows=[CaptureBeforeReview],
        activities=[b.prepare, b.extract, b.publish, b.failure],
        # The earlier workflow lives in this test module, which the sandbox cannot import.
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        old_run = await start(legacy)
        old = await temporal_env.client.start_workflow(
            CaptureBeforeReview.run,
            str(old_run.id),
            id=f"style-{old_run.id}",
            task_queue="style-legacy",
        )
        await asyncio.wait_for(old.result(), 30)
        old_history = await old.fetch_history()
    await Replayer(workflows=[StyleCaptureWorkflow]).replay_workflow(old_history)


@temporal_workflow.defn(name="style.capture")
class CaptureBeforeReview:
    """The capture workflow as it ran before review, to replay its histories."""

    @temporal_workflow.run
    async def run(self, run_id: str) -> None:
        for step in ("style_prepare", "style_extract", "style_publish"):
            await temporal_workflow.execute_activity(
                step,
                run_id,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
